# SDK Session Persistence

## Purpose

Claude SDK sessions store conversation state files in `~/.claude/` inside the container. Without a persistent volume mount for this directory, every Docker container rebuild wipes all session state, breaking session resumption and requiring users to manually recover every in-flight session after a system update.

This aspect documents how the platform persists the Claude SDK session directory across container rebuilds and why the `claude_sessions/` directory plays a dual role: it holds both SDK session data and the Claude Code hooks configuration that activates the credential access guard.

## Volume Mount Strategy

Each agent environment container has four volume mounts:

| Host Path | Container Path | Mode | What It Contains |
|-----------|---------------|------|-----------------|
| `${HOST_INSTANCE_DIR}/app/core` | `/app/core` | read-only | System server code — replaced on rebuild |
| `${HOST_INSTANCE_DIR}/app/workspace` | `/app/workspace` | read-write | User workspace — scripts, files, credentials, databases |
| `${HOST_INSTANCE_DIR}/claude_sessions` | `/root/.claude` | read-write | Claude SDK session files + `settings.json` |
| `${HOST_INSTANCE_DIR}/opencode_sessions` | `/root/.local/share/opencode` | read-write | OpenCode SQLite DB, logs, and session diffs |

The `claude_sessions/` and `opencode_sessions/` directories are created on the host when the environment instance is first set up (in `_copy_template()`). Because they are mounted rather than baked into the container image, they survive `docker-compose down` → `docker-compose up` cycles that make up a rebuild.

This mount is present in all three environment templates:
- `backend/app/env-templates/general-env/docker-compose.template.yml`
- `backend/app/env-templates/python-env-advanced/docker-compose.template.yml`
- `backend/app/env-templates/general-assistant-env/docker-compose.template.yml`

## What Survives a Rebuild vs What Is Replaced

| Directory | Survives Rebuild | Explanation |
|-----------|-----------------|-------------|
| `app/workspace/` | Yes | Docker volume; user-generated content is never deleted |
| `claude_sessions/` | Yes | Docker volume; Claude Code SDK session files and `settings.json` persisted |
| `opencode_sessions/` | Yes | Docker volume; OpenCode SQLite DB (sessions, messages, parts) and storage persisted |
| `app/core/` | No | Replaced on every rebuild from the shared `app_core_base`; contains server code |
| Container image layers | No | Rebuilt from the Dockerfile on every rebuild |

Because `claude_sessions/` persists, the Claude CLI can locate its session files at `/root/.claude/` after a new container is created from a fresh image, and session resumption succeeds without manual recovery.

## Session Resumption Flow

The Claude Code SDK identifies sessions by an opaque string ID. The flow across multiple messages:

1. **First message** — Backend sends `session_id=None` to the agent-env `/chat/stream` endpoint.
2. **SDK creates a new session** — `ClaudeCodeAdapter` passes no `--resume` flag. The CLI creates a new session and emits a session ID in the first response event.
3. **Session ID captured** — `ClaudeCodeAdapter._extract_session_id()` captures the ID from the response stream. `SessionService.set_external_session_id()` persists it in `session.session_metadata["external_session_id"]` in the database.
4. **Follow-up messages** — Backend reads `external_session_id` from `session_metadata` and passes it to the agent-env in the request payload.
5. **SDK resumes the session** — `ClaudeCodeAdapter` sets `options.resume = session_id` before calling the CLI. The CLI looks up `/root/.claude/<session_id>` (the mounted `claude_sessions/` directory) and resumes the conversation with its prior tool state and context.

If the session files are present (directory was persisted), resumption is transparent. If they are absent (e.g., pre-fix behavior where the directory was inside the container image), the CLI raises an error and the session falls into the recovery path described in [Session Recovery](../agent_commands/session_recovery_command.md).

### OpenCode Session Resumption

OpenCode stores all session state in a SQLite database at `/root/.local/share/opencode/opencode.db` inside the container. The database contains sessions, messages, message parts, and session diffs. The `opencode serve` process reads and writes this database during its lifecycle.

1. **First message** — Backend sends `session_id=None`. `OpenCodeAdapter._create_session()` calls `POST /session` on the OpenCode serve instance, which creates a new session record in the SQLite DB and returns a `ses_` ID.
2. **Session ID captured** — Same as Claude Code: the ID is persisted in `session.session_metadata["external_session_id"]`.
3. **Follow-up messages** — Backend passes the `ses_` ID. `OpenCodeAdapter` calls `POST /session/{id}/message` on the serve instance, which looks up the session in the SQLite DB.
4. **After rebuild** — Because `opencode_sessions/` is mounted to `/root/.local/share/opencode`, the SQLite DB survives. When OpenCode serve starts in the new container, it opens the existing DB and can resume sessions by their `ses_` IDs.

Note: `GET /global/event` is a serve-wide SSE stream shared by every session on the mode's process. The adapter demultiplexes it by session ID so events from a different (e.g. orphaned) session are dropped before they reach the current stream. See [multi_sdk_tech.md](../agent_environment_core/multi_sdk_tech.md) for the full filtering logic.

## Hooks Settings File

The Claude Code credential guard hook requires a `settings.json` at the Claude CLI's "user" settings path. That path is `~/.claude/settings.json`, which inside the container resolves to `/root/.claude/settings.json`.

`_write_claude_code_hook_settings()` in `EnvironmentLifecycleManager` writes this file to `instance_dir/claude_sessions/settings.json` — the host-side path that maps to `/root/.claude/settings.json` in the container.

The file activates a `PreToolUse` hook for `Bash|Read|Write|Edit` tool calls:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Read|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /app/core/hooks/credential_guard_hook.py"
          }
        ]
      }
    ]
  }
}
```

This method is called from `_update_environment_config()`, which runs on every environment creation, rebuild, and start. If `settings.json` already contains entries (e.g., from a prior configuration), the method merges the credential guard entry rather than overwriting — existing hooks are preserved.

Note: Before this fix, `settings.json` was written to `app/core/.claude/settings.json`. That path was inside the read-only core mount, which the Claude CLI never checked for user-scoped settings. The hook was silently ignored on every session.

## Integration With the Rebuild Flow

During a rebuild (`rebuild_environment()`):

1. Container is stopped and removed (`docker-compose down`).
2. `app/core/` is deleted and replaced from the shared `app_core_base`.
3. Infrastructure files (Dockerfile, pyproject.toml, etc.) are overwritten from the template.
4. Docker image is rebuilt.
5. `_update_environment_config()` is called — this includes `_write_claude_code_hook_settings()`, which rewrites `claude_sessions/settings.json` with the current hook configuration (merging if the file already exists from the previous build).
6. New container is started. The `claude_sessions/` volume is re-attached. Existing SDK session files are present.

The result: SDK session resumption works immediately after a rebuild without user-facing interruption, provided the environment's existing sessions had their `external_session_id` stored before the rebuild.

## Lifecycle: When the Directory Is Created

`_copy_template()` creates both `instance_dir/claude_sessions/` and `instance_dir/opencode_sessions/` when a new environment instance is first set up. These directories start empty; SDK session files are added during the first chat.

For existing environments created before `opencode_sessions/` was added, `_update_environment_config()` also ensures the directory exists (called on every rebuild and start), so the volume mount works retroactively.

If a directory already exists (e.g., re-running setup on an existing instance), `mkdir(parents=True, exist_ok=True)` is a no-op and existing files are not disturbed.

## Integration Points

- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — The `ClaudeCodeAdapter` inside the container passes `options.resume` to the CLI, which reads session files from `/root/.claude/`. Without the persistent mount, the CLI cannot find those files after a rebuild.
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — `session.session_metadata["external_session_id"]` is the database-side store for the SDK session ID. The backend reads this on every follow-up message and sends it to the agent-env. The persistence of the file on disk and the persistence of the ID in the database must both be present for resumption to work.
- **[Session Recovery](../agent_commands/session_recovery_command.md)** — When resumption fails (e.g., for sessions created before the volume mount was added), the recovery command clears `external_session_id` from `session_metadata` and sets `recovery_pending`, causing the next message to create a fresh SDK session with reconstructed context.
- **[Multi-Image Environments](./agent_multi_image_environments.md)** — All three environment templates (`general-env`, `python-env-advanced`, `general-assistant-env`) include the `claude_sessions` volume mount. Template selection does not affect session persistence behavior.
