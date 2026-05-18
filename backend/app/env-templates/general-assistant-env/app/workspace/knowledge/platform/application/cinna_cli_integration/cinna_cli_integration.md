# Cinna CLI Integration

## Purpose

Enables local development of remote agents using the `cinna` CLI tool. Users develop agent scripts, prompts, and webapps using local editors and AI coding tools (Claude Code, opencode, Cursor). The local machine syncs files continuously with the remote agent environment via Mutagen; the remote environment is the only runtime. Credentials never leave the platform.

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Bootstrap Script** | Python script served by the platform at `GET /api/cli-setup/{token}`. Checks if `cinna` is installed — if yes, delegates to `cinna setup`; if no, prints install instructions |
| **Setup Token** | Short-lived (15min), single-use token embedded in the bootstrap URL. Generated from the UI, consumed when `cinna setup` exchanges it for a CLI token |
| **CLI Token** | Long-lived JWT (7-day rolling expiry) stored on the user's machine. Created by exchanging a setup token. Supports revocation from the UI |
| **Live Sync** | Continuous bidirectional workspace file sync between the user's machine and the remote agent environment, powered by Mutagen over an HTTP-tunnelled WebSocket |
| **Foreground Dev Session (`cinna dev`)** | The primary developer workflow — starts (or reuses) the Mutagen sync session and attaches the terminal to a two-tab TUI (status + raw Mutagen details). Ctrl-C terminates the TUI _and_ the sync session; nothing is left running in the background |
| **Agent Registry** | Per-machine JSON at `~/.cinna/agents.json` mapping `agent_id → {platform_url, cli_token, frontend_url, workspace_path}`. Source of truth for `cinna list` and for the SSH shim that Mutagen's shared daemon invokes on every agent connection |
| **MCP Proxy** | Local MCP server (stdio) that forwards knowledge queries from local AI tools to the platform's knowledge search API |
| **Building Context** | The assembled building-mode prompt pulled from the env core, bundled with companion prompt files (`WEBAPP_BUILDING.md`, `COMPLEX_AGENT_DESIGN.md`, …) shipped inline in the response — makes local AI tools behave like the platform's building agent and lets them follow the same on-demand guides without needing the Docker build context locally |

## User Stories / Flows

### 1. Setting Up Local Development

1. User navigates to agent's **Integrations** tab
2. Clicks **Setup** in the Local Development card
3. Platform generates a setup token and displays a `curl | python3` oneliner with inline **Copy** and **Regenerate** buttons
4. User copies the command and runs it in their terminal
5. Platform serves a bootstrap script (`GET /api/cli-setup/{token}`) that checks if `cinna` CLI is installed:
   - **If installed**: runs `cinna setup <url>` which exchanges the token, verifies/installs the correct Mutagen version, downloads the initial workspace tarball, writes `CLAUDE.md` + `BUILDING_AGENT.md` + companion prompt guides, starts the live sync session, and attaches the foreground TUI (Ctrl-C to detach and stop sync)
   - **If not installed**: prints install instructions (`uv tool install cinna-cli` or `pip install cinna-cli`) and exits
6. User records the agent in `~/.cinna/agents.json` and moves into the agent directory; the setup prints a short "next steps" block with the most common commands
7. From there, `cinna dev` resumes a foreground dev session at any time; closing the TUI stops the sync but leaves workspace files and tokens intact

### 2. Local Development Workflow

1. User runs `cinna dev` inside the agent workspace — starts (or reuses) the Mutagen sync session and attaches a two-tab TUI (Tab 1: friendly status + activity log; Tab 2: raw `mutagen sync list --long` output). Left/right arrows switch tabs; Ctrl-C or `q` detaches _and_ stops the sync
2. User edits scripts and prompts locally in their editor; Mutagen syncs changes to the remote environment in near-real time (bidirectional, sub-second latency)
3. Remote environment is the authoritative runtime — same Python packages, system deps, credentials as production
4. `cinna exec <command>` runs the command in the remote environment (from another terminal while `cinna dev` is live, or standalone — it does not require an active dev session) and streams stdout/stderr back
5. Local AI tools (Claude Code, Cursor) read `CLAUDE.md` and `BUILDING_AGENT.md` for agent context; companion guides (`WEBAPP_BUILDING.md`, `COMPLEX_AGENT_DESIGN.md`, …) sit next to `BUILDING_AGENT.md` so the on-demand `./<NAME>.md` references in the building prompt resolve locally — letting the local assistant follow the same webapp-build and complex-agent-design workflows the platform's building agent does
6. MCP proxy provides `knowledge_query` tool for searching the agent's knowledge base
7. When any watched workspace file (`docs/WORKFLOW_PROMPT.md`, `docs/ENTRYPOINT_PROMPT.md`, `docs/REFINER_PROMPT.md`, `docs/CLI_COMMANDS.yaml`, `app-data/storage/STATUS.md`) changes and stabilises — e.g., after a Mutagen sync completes — env-core fires a single callback and the backend emits `WORKSPACE_FILES_CHANGED`; downstream handlers resync agent prompts, refresh the CLI commands cache, and pull the STATUS.md snapshot. This is the same post-action refresh that runs after stream completion <!-- nocheck -->

### 3. Managing Active Sessions (UI)

1. Integrations tab shows list of active CLI sessions (machine name, last sync time)
2. Session row shows sync status: "Synced <relative time>" (recent activity) or "Idle <relative time>"
3. User can **Disconnect** a session — revokes the CLI token immediately
4. Next CLI sync action with the revoked token gets a 401; Mutagen pauses and the CLI TUI surfaces the error
5. Local files remain intact — only the authentication is invalidated

### 4. Managing Multiple Agents on One Machine

1. Every `cinna setup` registers the agent in `~/.cinna/agents.json` alongside credentials, the web-UI URL, and the workspace path
2. `cinna list` prints a table of registered agents with agent name, agent ID, web-UI link (`<frontend_url>/agent/<id>`), workspace path, and live sync state (active / connecting / paused / error / idle)
3. Workspace directories that were deleted outside the CLI appear as `missing:` in the list — `cinna disconnect` (from the parent directory) cleans the stale registry entry
4. `cinna disconnect` inside an agent workspace stops the sync session, removes `.cinna/` and auto-generated files (`CLAUDE.md`, `BUILDING_AGENT.md`, `mutagen.yml`, `.mcp.json`, `opencode.json`, companion prompt guides), and drops the registry entry; the rest of the workspace is preserved
5. `cinna disconnect-all` scans the current directory and removes every agent workspace found under it — used to reset a dev folder

### 5. Token Lifecycle

1. Setup token expires after 15 minutes or first use (whichever comes first)
2. CLI token expires after 7 days of **inactivity** (rolling window renewed on each API call and sync WebSocket connection)
3. User can regenerate setup tokens at any time (previous unused ones still work until they expire)
4. Expired setup tokens are cleaned up automatically by a background scheduler (hourly)

## Business Rules

### Authentication

- Setup tokens are single-use — re-exchange returns 400
- CLI tokens are scoped to exactly one agent and one user
- CLI JWT includes `token_type: "cli"` to distinguish from regular user JWTs
- Token hash (SHA-256) stored in DB — actual token value shown only once at creation
- Rolling expiry: every successful API call (and sync WebSocket connect) renews the 7-day window
- Agent deletion cascades to CLI token deletion

### Authorization

| Resource | Rule |
|----------|------|
| Setup token creation | Authenticated user who owns the agent |
| Workspace (initial clone) | CLI token owner must own the agent |
| Live sync WebSocket | CLI token owner must own the agent |
| Remote exec | CLI token owner must own the agent |
| Building context | CLI token owner must own the agent |
| Knowledge search | CLI token owner must own the agent |
| Token revocation | Token owner only |

### Live Sync

- Mutagen operates in `two-way-safe` conflict mode — conflicting edits produce `.conflict.*` copies; conflicts are surfaced in the CLI TUI, not the web UI
- Default ignore set: `.git`, `__pycache__`, `node_modules`, `.venv`, `.cinna/`, `.mypy_cache/`
- Sync keeps the environment warm. While a sync WebSocket is connected, the auto-suspend scheduler skips the environment
- After the last sync WebSocket disconnects, a grace period (default 5 minutes) starts; if no new sync or session activity arrives before it elapses, the environment follows its normal auto-suspend path
- Mutagen version is pinned per platform release. `cinna setup` and `cinna sync start` verify the local Mutagen version matches; a version mismatch fails fast with an install command

### Workspace Files Resync

- Env-core runs a lightweight mtime-poll watcher over `docs/WORKFLOW_PROMPT.md`, `docs/ENTRYPOINT_PROMPT.md`, `docs/REFINER_PROMPT.md`, `docs/CLI_COMMANDS.yaml`, and `app-data/storage/STATUS.md`. When any of them stabilises after a change (5-second stable window), env-core POSTs `workspace-files-changed` to the backend with the list of changed paths <!-- nocheck -->
- The backend emits `WORKSPACE_FILES_CHANGED`; three handlers are registered on it:
  - `EnvironmentService.handle_workspace_files_changed_event` — `sync_agent_prompts_from_environment()` (A2A skills regen + background description update when `workflow_prompt` actually changes)
  - `CLICommandsService.handle_post_action_event` — refreshes the cached `CLI_COMMANDS.yaml` (rate-limited per-env to 30s)
  - `AgentStatusService.handle_post_action_event` — pulls the latest `STATUS.md` snapshot
- The same three handlers already subscribe to `STREAM_COMPLETED` / `STREAM_ERROR` / `CRON_*`, so a Mutagen-sync resync is now indistinguishable from an end-of-session resync
- `POST /workspace` push is gone — continuous Mutagen sync is the only path. The legacy `prompt-file-changed` callback is retained as an alias so agent environments built before the generic watcher shipped keep working without a rebuild

### Credentials

- Credentials are never written to the user's machine. The remote environment holds all credentials; `cinna exec` runs commands in that environment where credentials are already available via `workspace/credentials/credentials.json`

### Remote Exec

- `cinna exec` streams stdout and stderr in real time as each chunk arrives — output is not buffered until the command finishes, so long-running scripts produce visible progress at print time
- Default timeout is **1800 seconds (30 minutes)**. Override per-invocation with `--timeout / -t SECS` (range 1–86400, i.e. 1 second to 24 hours)
- When the timeout expires the remote subprocess is killed (SIGKILL); the CLI receives a `done` event marked `timed_out: true` and exits non-zero
- If the user's own command takes a `--timeout` flag, separate it with `--`: `cinna exec --timeout 3600 -- python tool.py --timeout 30`
- Three terminal events from the remote: `done` (normal completion or timeout — carries the exit code), `interrupted` (output truncated at the 256 KB byte cap), `error` (subprocess failed to start)
- Output is hard-capped at 256 KB per invocation. Output beyond that is truncated and the subprocess is terminated

## CLI Commands

| Command | Purpose |
|---------|---------|
| `cinna setup <token_or_url>` | Exchange a setup token, bootstrap the workspace, and attach a foreground TUI. The primary entry point from the platform's Integrations tab |
| `cinna dev` | Start (or reuse) the Mutagen sync session and attach a two-tab TUI. Ctrl-C stops sync and detaches. The primary developer loop |
| `cinna exec [--timeout SECS] <command>` | Run a command in the remote environment; streams stdout/stderr and returns the remote exit code. Default timeout 1800 s (30 min); raise with `--timeout` / `-t` for longer jobs |
| `cinna status` | One-shot snapshot of agent info and sync state for the current workspace |
| `cinna sync status` | Read-only view of the live sync session state (safe to run from a second terminal while `cinna dev` is attached) |
| `cinna sync conflicts` | List Mutagen `.conflict.*` files; the user resolves them by editing in place |
| `cinna list` | Table of every agent registered on this machine with agent ID, web-UI link, workspace path, and sync state |
| `cinna disconnect` | Stop sync and remove `.cinna/` plus auto-generated files for the current workspace; preserves the rest |
| `cinna disconnect-all` | Remove every cinna workspace found under the current directory |

The removed `cinna sync start / stop / pause / resume` commands no longer exist — the foreground `cinna dev` model replaced the daemonised lifecycle.

## Architecture Overview

```
User's IDE          cinna CLI / Mutagen         Platform Backend        Agent env (Docker)
    |                       |                          |                       |
    |-- edits files ----▶  ./workspace/               |                       |
    |                       |                          |                       |
    |                       |◀── bidirectional sync ──▶|── WS proxy ──────▶  /workspace/
    |                       |    (WS tunnel over HTTPS)|                       |
    |                       |                          |                       |
    |                       |── cinna exec ──────────▶| POST /exec ─────────▶ /command/stream
    |                       |                          |                       |
    |              MCP proxy (stdio)                   |                       |
    |◀── knowledge_query ───|── HTTP ────────────────▶|── knowledge search ──▶|
                           │
                           │ reads / writes on every agent
                           ▼
                  ~/.cinna/agents.json
                  (global per-user registry —
                   one Mutagen daemon serves all
                   agents; SSH shim picks the
                   right token per connection)
```

## Integration Points

- **Agent Management** — CLI tokens are scoped per-agent, linked via FK. See [agent_management](../agent_management/agent_management.md)
- **Agent Environments** — The sync WebSocket keeps environments warm; a grace-period timer governs auto-suspend after disconnect. See [agent_environments](../../agents/agent_environments/agent_environments.md)
- **Agent Environment Core** — `WS /sync/exec` spawns `mutagen-agent` inside the container; `POST /command/stream` streams exec output. See [agent_environment_core](../../agents/agent_environment_core/agent_environment_core.md)
- **Knowledge Sources** — MCP proxy calls existing vector search infrastructure. See [knowledge_sources](../knowledge_sources/knowledge_sources.md)
- **Frontend Integrations Tab** — LocalDevCard sits alongside A2A, MCP, Access Token cards in the agent detail page

## Aspects

- [Local CLI Development](local_cli_development.md) — How to develop and test the `cinna` CLI tool itself against a local platform instance (editable install from source)
