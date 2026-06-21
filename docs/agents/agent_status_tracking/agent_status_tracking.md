# Agent Status Tracking

## Purpose

Lightweight, self-published heartbeat for every agent: the agent (or its scripts) writes a `STATUS.md` file under the per-install **App Data** storage area, and the platform surfaces its contents through a slash command, REST endpoint, agent-card footer in the agents list, and A2A method. Lets users and external monitors see "what's this agent currently doing / is it healthy" without invoking the LLM.

The file lives in `app-data/storage/`, **not** in the bundle-owned `docs/` folder, because status reflects the runtime health of a specific install (its credentials, data, scheduled checks) rather than something the publisher ships in the bundle. App Data is the per-user, per-bundle persistent volume that survives apply-update and uninstall/reinstall, so an install's status history is never wiped by a bundle revision push.

## Core Concepts

- **STATUS.md** — a markdown file at `/app/workspace/app-data/storage/STATUS.md` inside the agent environment. Always reflects the *current* state; agents overwrite it in place rather than appending.
- **Frontmatter (optional)** — YAML block with `timestamp`, `status`, `summary` keys. When present, the platform extracts structured metadata; otherwise the file is treated as freeform.
- **Severity** — one of `ok`, `warning`, `error`, `info`, or `unknown` (anything unrecognized normalizes to `unknown`).
- **Snapshot** — the cached parsed result stored on the `agent_environment` row, so status remains visible even when the environment is stopped.
- **Severity transition** — when the parsed severity differs from the previous fetch. Transitions emit a WebSocket event and create an activity-feed entry.
- **Reported-at source** — `frontmatter` (timestamp came from YAML), `file_mtime` (fallback to file modification time), or `null`.
- **Status refresh command** — an optional shell command (or `/run:<name>` CLI command reference) stored on the agent that runs inside the container immediately before any live/forced status fetch. Lets the agent update `STATUS.md` on demand before the platform reads it. Configured via the **Configuration tab > Agent status card**; default value is `/run:status`. Non-blocking: failures emit a transient warning that is surfaced in the status response but never block the STATUS.md read.
- **Force refresh (single entrypoint)** — every user-initiated refresh (UI Refresh button, REST `?force_refresh=true`, A2A `agent/status` force, `/agent-status` command) goes through one service method, `AgentStatusService.force_refresh_status`. It wakes a suspended environment, runs the pre-command, fetches `STATUS.md`, and on any failure returns the cached snapshot with the warning attached — it never raises. This replaces the per-caller try/fetch/fallback that used to be copy-pasted across the three surfaces.
- **Suspended-env auto-resume** — on a force path, if the agent's environment is `suspended` (auto-stopped for inactivity), the platform activates it first (the same `activate_suspended_environment` wake-up used by message-send / CLI / sessions) so the pre-command and `STATUS.md` read run against a live container instead of silently serving a stale cache. Best-effort: a wake-up failure falls through to the existing "environment not running" warning + cached snapshot.
- **Refresh command warning** — a transient, non-persisted string attached to force/live fetch results when the pre-command did not run cleanly (missing `/run:` reference, non-zero exit, timeout, env down). Always `null` on cache-only snapshots. Exposed on `AgentStatusPublic.refresh_command_warning`, the A2A `agent/status` result, and in the `/agent-status` slash command output.

## User Stories / Flows

### 1. Agent publishes its status
1. Agent (or a scheduled OK-pattern script) calls the bundled helper: `python scripts/update_status.py --status ok --summary "All clear"`.
2. The helper atomically writes `STATUS.md` (write to `.tmp` then `os.replace`).
3. The next backend-triggered action in the env (a session stream completing, a CRON run finishing) pulls the new contents via the post-action handler. Slash-command / force-refresh / A2A callers pick up the change on demand.

### 2. User views status from the agents list
1. Agents list page loads. The grid batch-fetches all snapshots in one call via `GET /api/v1/agents/status?workspace_id=…` and routes each one to its `AgentCard`.
2. Cards that received a non-empty snapshot render a compact `AgentStatusCardFooter`: colored severity dot, summary (ellipsized), relative timestamp from the agent's own `reported_at`. Cards with no published status omit the footer entirely.
3. Clicking the footer opens `AgentStatusDialog` with the full markdown body, header strip (severity, summary, reported-at, fetched-at, optional transition line), refresh + copy buttons. The card's main link is not triggered.

### 3. User runs `/agent-status` in chat
1. User types `/agent-status` (autocompleted from the command registry).
2. Backend renders a markdown response: severity icon + summary header line, `Reported …  fetched …` timestamps, divider, body (frontmatter stripped).
3. No LLM call is made — pure command output.

### 4. Agent owner configures the status refresh command
1. Owner opens the agent's **Configuration tab** and finds the **Agent status** card (alongside Schedules and Handovers).
2. The card shows the current cached snapshot (severity dot, summary, reported/fetched timestamps) and a **Refresh** button.
3. Owner edits the **Status refresh command** input — either a raw shell/Python command or `/run:<name>` referencing a command declared in `CLI_COMMANDS.yaml`. Default is `/run:status`.
4. Owner clicks **Save**; the value is persisted via `PATCH /agents/{id}` (field: `status_refresh_command`).
5. A **Reset to default** button restores `/run:status` without saving.
6. Clicking **Refresh** immediately triggers a force refresh: if the environment is suspended it is woken first, then the configured pre-command runs and `STATUS.md` is re-read. Any warning from the pre-command appears in a backend-authoritative amber banner above the command input.
7. The card is not shown to users with the `agent-user` role.

### 5. External monitor polls REST
1. External agent calls `GET /api/v1/agents/{id}/status` with a bearer token (user JWT, A2A token, or desktop auth).
2. Receives the structured `AgentStatusPublic` snapshot including `refresh_command_warning` when applicable.
3. Optional `?force_refresh=true` bypasses the cache, wakes the environment if it is suspended, and runs the configured pre-command before reading STATUS.md. (The REST surface does not rate-limit force refresh — user-initiated refreshes always fetch; the per-env 30 s limit governs background/event pulls and the A2A force path.)

### 6. A2A peer queries status
1. Peer calls JSON-RPC method `agent/status` against `/api/v1/a2a/{agent_id}/`.
2. Receives the same payload shape as the REST response; `refresh_command_warning` is included in the result dict.
3. The `status` skill is declared on the agent's A2A card.
4. Force paths inside A2A run the pre-command before reading STATUS.md.

### 7. Post-action pull after every backend-triggered agent-env action
1. The agent-env has no outbound network access, so the backend is the only actor that knows when an in-container action just finished.
2. Every such finish emits an event — session streams emit `STREAM_COMPLETED` / `STREAM_ERROR`; the CRON scheduler emits `CRON_COMPLETED_OK` (OK-pattern script returned "OK"), `CRON_TRIGGER_SESSION` (schedule started a session), or `CRON_ERROR` (schedule failed). The env-core file watcher also emits `WORKSPACE_FILES_CHANGED` whenever `app-data/storage/STATUS.md` stabilises after a write.
3. `AgentStatusService.handle_post_action_event` is registered against **seven** events (the six above plus `ENVIRONMENT_ACTIVATED` — see flow 8 below). It reads `environment_id` from the event meta and calls `refresh_after_action(env)`.
4. `refresh_after_action` honors the 30 s per-env rate-limit so bursts collapse to a single fetch. Errors are swallowed — status tracking is best-effort.
5. When the event names `app-data/storage/STATUS.md` in `meta.changed_files`, the handler passes `force=True` to bypass the rate limit. We have direct evidence the file changed (the watcher debounces at ≥5 s), so the de-dup heuristic that protects against speculative bursts no longer applies — mirroring the auto-sync semantics the prompt files use.
6. Post-action event handlers call `fetch_status` with `run_refresh_command=False` — the pre-command is never invoked on these background paths.

### 8. Pull on environment activation (gap fix)
1. Previously, `AgentStatusService.handle_post_action_event` was not registered for `ENVIRONMENT_ACTIVATED`. This meant that on a fresh env start with no streaming action, the status cache could remain stale until the first session or CRON event.
2. `ENVIRONMENT_ACTIVATED` is now included in the post-action event set for all pull-only synced files, closing this gap. The 30 s rate limit still dedupes bursts.
3. Additionally, `_sync_dynamic_data` (the env start/activate sweep that runs before `ENVIRONMENT_ACTIVATED` is emitted) now also calls `AgentStatusService.refresh_after_action(force=True)` directly, so STATUS.md is current at the moment the environment becomes active — independent of the event handler firing afterward.
4. Activation-path refreshes are background pulls (`run_refresh_command=False`) — the pre-command never runs on these paths.

## Business Rules

- **File location is fixed** — only `/app/workspace/app-data/storage/STATUS.md` is read. No per-skill or nested status files in MVP.
- **Frontmatter is optional** — agents may publish freeform markdown; severity will normalize to `unknown` and the summary falls back to the first non-blank, non-heading body line.
- **Severity vocabulary is closed** — only `ok`, `warning`, `error`, `info` are recognized. Other values become `unknown`.
- **Size cap** — body truncated at 64 KB with a `... (truncated)` marker; frontmatter rejected if > 4 KB.
- **Timestamp resolution** — frontmatter `timestamp` wins when valid; otherwise the file's mtime; otherwise `null`. The chosen source is exposed as `reported_at_source`.
- **Severity transitions are first-class** — first-ever fetch counts as a transition from `null`. Transitions update `prev_severity` + `severity_changed_at`, emit `agent_status_updated`, and create an activity-feed entry.
- **Rate limit on force-refresh** — one live fetch per environment per 30 s. REST returns `429`; the slash command silently serves cached data.
- **Status refresh command runs only on live/force paths** — the pre-command executes before the file read exclusively on `GET /agents/{id}/status?force_refresh=true`, A2A `agent/status` (force), and the `/agent-status` slash command. All three go through the single `force_refresh_status` service entrypoint. Post-action background refreshes, `refresh_after_action`, and the agents-list batch endpoint never run the pre-command.
- **Force paths wake a suspended env; background paths do not** — `force_refresh_status` activates a `suspended` environment before running the pre-command/fetch, so a forced refresh updates a sleeping install instead of returning stale cache. Only `suspended` envs are woken (a heavier `stopped`/`error` start is never triggered by a status poll). Event-driven/post-action pulls (`run_refresh_command=False`) never wake an env — they only read whatever is already running.
- **Pre-command resolution: `/run:<name>` references** — the name is looked up in `AgentEnvironment.cli_commands_parsed` (the cached CLI_COMMANDS.yaml list). If the name is not found, a transient warning is returned and execution is skipped; the STATUS.md read still proceeds. Blank/`None` command means deliberate opt-out — no warning is generated.
- **Pre-command is non-blocking** — env down, non-zero exit, timeout (120 s), connect error, or unexpected error all produce a warning string instead of blocking the STATUS.md fetch. Warnings never include stdout/stderr to prevent leakage into public-ish status surfaces.
- **Refresh command warning is transient** — never persisted to the DB. Present only on live/force fetch results; always `null` on cached snapshots.
- **No built-in staleness concept** — update cadence is agent-specific (some run every minute, some once a week). The UI shows the agent's own `reported_at`; downstream consumers decide what "too old" means for their domain.
- **No secrets in STATUS.md** — agents are explicitly instructed never to write credential values; treated as a public artifact (rendered to UI, returned via API, included in A2A responses).
- **Cache survives env stop** — snapshot fields stay on the `agent_environment` row until the env is deleted; users see the last published state.

## Architecture Overview

```
Agent script ──writes──▶ /app/workspace/app-data/storage/STATUS.md
                              │
                              ▼ (pulled only when a backend-triggered
                                 action completes, or on REST/A2A/cmd)
              ┌───────────────┴───────────────┐
              │     AgentStatusService        │
              │  ┌─────────────────────────┐  │
              │  │ force_refresh_status    │  │  ← single force entrypoint
              │  │  └ wraps fetch_status,  │  │    (UI / REST / A2A / cmd)
              │  │    falls back to cache  │  │    never raises
              │  │ fetch_status            │  │
              │  │  ├ [run_refresh_command]│  │  ← force/live paths only
              │  │  │   _ensure_environment_running│ ← wakes suspended env
              │  │  │   _run_refresh_command│ │
              │  │  │   _resolve_run_command│ │  ← /run:<name> → CLI_COMMANDS.yaml
              │  │  ├ adapter.fetch_..._with_meta
              │  │  ├ parse_status_file   │  │
              │  │  ├ resolve_reported_at │  │
              │  │  ├ detect transition   │  │
              │  │  ├ persist snapshot    │  │
              │  │  └ emit event + activity│ │
              │  └─────────────────────────┘  │
              │  ┌─────────────────────────┐  │
              │  │ handle_post_action_event│  │
              │  │  ← STREAM_COMPLETED     │  │  run_refresh_command=False
              │  │  ← STREAM_ERROR         │  │
              │  │  ← CRON_COMPLETED_OK    │  │
              │  │  ← CRON_TRIGGER_SESSION │  │
              │  │  ← CRON_ERROR           │  │
              │  └─────────────────────────┘  │
              └───────────────┬───────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 GET /agents/{id}/status   /agent-status cmd    A2A agent/status
  (force_refresh=true        (markdown reply      (JSON-RPC; force
   → pre-command runs)        + warning line)      → pre-command runs)
   GET /agents/status       
   (cache-only, no cmd)
                              │
                              ▼
                    AgentCard Footer / Dialog
                    (agents list, WS-driven)

                    AgentStatusCard (Configuration tab)
                    (snapshot + command input + Refresh button)
```

## Integration Points

- **[Agent Commands](../agent_commands/agent_commands.md)** — registers the `/agent-status` slash command via `CommandService`. Full command spec in [`agent_status_command.md`](../agent_commands/agent_status_command.md). The slash command is one of the three live/force paths that run the pre-command.
- **[CLI Commands](../cli_commands/cli_commands.md)** — the status refresh command can reference a named CLI command via `/run:<name>`, resolved against `AgentEnvironment.cli_commands_parsed` (the cached `CLI_COMMANDS.yaml` list). A missing `/run:` reference emits a transient warning and skips execution; the STATUS.md read still proceeds.
- **[Agent Environments](../agent_environment_core/agent_environment_core.md)** — extends the workspace adapter with `fetch_workspace_item_with_meta()`. `AgentEnvConnector().exec_command()` is used to run the pre-command (the same non-streaming exec path the scheduler uses). On force paths a suspended env is first woken via `EnvironmentLifecycleManager.activate_suspended_environment()` — the same wake-up message-send / CLI / sessions use; activation rotates the env auth token, so the refreshed `status`/`config` are copied back onto the in-memory env before the pre-command/fetch.
- **[A2A Integration](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — exposes the `status` skill and `agent/status` JSON-RPC method on the A2A agent card. A2A force paths run the pre-command; `refresh_command_warning` is included in the result dict.
- **[Agent Management](../../application/agent_management/agent_management.md)** — the `status_refresh_command` field is part of `AgentUpdate` and `AgentPublic`; the Configuration tab hosts the `AgentStatusCard` (in the operational-settings section, alongside Schedules and Handovers) where owners view the current snapshot and configure the command. Card is shown only behind the developer-tier `showOperationalSettings` gate — hidden for `agent-user` role visitors and on foreign (consumer) installs.
- **Activity feed** — severity transitions create an entry visible in the agent's activity timeline.
- **Event bus (outbound)** — emits `agent_status_updated` events consumed by the WebSocket bridge and frontend React Query invalidation.
- **Event bus (inbound)** — subscribes `handle_post_action_event` to `ENVIRONMENT_ACTIVATED`, `STREAM_COMPLETED`, `STREAM_ERROR`, `CRON_COMPLETED_OK`, `CRON_TRIGGER_SESSION`, `CRON_ERROR`, and `WORKSPACE_FILES_CHANGED` (7 events total). The `ENVIRONMENT_ACTIVATED` subscription was added to close a gap where STATUS.md was not pulled when an environment started without any immediately following stream or CRON action. Handler wiring is driven by the **Synced Workspace File Registry** (`synced_files.py`) in `backend/app/main.py`; no hand-editing of individual registration blocks is needed. The CRON events are emitted by `agent_schedule_scheduler._emit_cron_event` at every schedule-execution exit point. `WORKSPACE_FILES_CHANGED` carries `meta.changed_files`; when the list contains `app-data/storage/STATUS.md` the handler bypasses the 30 s rate limit. Status.md is also classified in the registry as a `pull_only` synced file. All event-handler paths call `fetch_status` with `run_refresh_command=False`.
- **App-core env template** — ships `workspace/scripts/update_status.py` (helper) which writes to `app-data/storage/STATUS.md`. No placeholder file is shipped with the bundle: the file is per-install state and the platform creates the `app-data/storage/` directory on install. No in-container watcher — the backend is the sole reader.
- **[Agent App Data](../agent_app_data/agent_app_data.md)** — `app-data/storage/` is created by `AppDataService.get_or_create_volume` at install time and bind-mounted into the container. It survives uninstall/reinstall and is never overwritten by `apply_update`.
- **COMPLEX_AGENT_DESIGN.md** — documents the convention for agent authors and cross-links from the OK-pattern scheduled-script section.
