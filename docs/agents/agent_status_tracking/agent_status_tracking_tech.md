# Agent Status Tracking — Technical Details

## File Locations

### Backend

**Models**
- `backend/app/models/agents/agent_status.py` — `AgentStatusPublic`, `AgentStatusListPublic` Pydantic response models
- `backend/app/models/environments/environment.py` — `AgentEnvironment` extended with eight `status_file_*` columns
- `backend/app/models/events/event.py` — `EventType.AGENT_STATUS_UPDATED` enum value

**Services**
- `backend/app/services/agents/agent_status_service.py` — core service with `fetch_status`, `parse_status_file`, `get_cached_status`, `is_stale`, `should_refresh`, `is_rate_limited`, `get_primary_environment`, `empty_snapshot`, `to_public`, plus `StatusUnavailableError`
- `backend/app/services/agents/commands/agent_status_command.py` — `AgentStatusCommandHandler` for the `/agent-status` slash command
- `backend/app/services/agents/commands/__init__.py` — registers the command handler in the command registry
- `backend/app/services/environments/environment_status_scheduler.py` — opportunistic snapshot refresh on healthy env pings
- `backend/app/services/environments/adapters/base.py` — `fetch_workspace_item_with_meta()` + `WorkspaceItemMeta` dataclass
- `backend/app/services/environments/adapters/docker_adapter.py` — single-GET implementation that parses `Last-Modified` / `Content-Length` / `Content-Type` headers before streaming the body

**Routes**
- `backend/app/api/routes/agent_status.py` — both public `router` and `internal_router` for status endpoints
- `backend/app/api/main.py` — registers `agent_status.router` *before* `agents.router` (so `/agents/status` doesn't get matched as an agent UUID) plus `agent_status.internal_router`
- `backend/app/api/routes/a2a.py` — `agent/status` JSON-RPC method handler and `status` skill on the agent card

**Migration**
- `backend/app/alembic/versions/34322f866173_add_agent_environment_status_fields.py` — adds eight nullable columns

**Tests**
- `backend/tests/api/agents/agents_status_test.py` — parser unit tests, timestamp resolution, severity transition, rate-limit, is_stale, REST + push endpoints
- `backend/tests/stubs/environment_adapter_stub.py` — `EnvironmentTestAdapter.workspace_files` class-level dict + `fetch_workspace_item_with_meta()` stub

### Frontend

**Components**
- `frontend/src/components/Agents/AgentStatusBadge.tsx` — session-header dot + summary + relative time + transition chip + stale state
- `frontend/src/components/Agents/AgentStatusDialog.tsx` — full markdown body + refresh + copy
- `frontend/src/routes/_layout/session/$sessionId.tsx` — mounts the badge in the session header

**Hooks & Services**
- `frontend/src/hooks/useAgentStatus.ts` — `useAgentStatus(agentId, dialogOpen)` React Query hook + `severityDotClass` / `severityLabel` / `isRecentTransition` helpers
- `frontend/src/services/eventService.ts` — `EventTypes.AGENT_STATUS_UPDATED` registered for WS dispatch

**Generated client**
- `frontend/src/client/sdk.gen.ts` — `AgentsService.getAgentStatus({ agentId, forceRefresh? })`, `AgentsService.listAgentStatuses({ workspaceId? })` (auto-generated; do not edit)

### Env Template (App Core / General Assistant)

- `backend/app/env-templates/app_core_base/core/prompts/COMPLEX_AGENT_DESIGN.md` — "Agent Self-Reported Status" section + cross-link from "Rules for OK-pattern scripts"
- `backend/app/env-templates/app_core_base/core/server/status_watcher.py` — agent-env mtime watcher (5 s poll, 2 s debounce, POSTs to backend internal endpoint)
- `backend/app/env-templates/app_core_base/core/main.py` — wires the watcher into the agent-env FastAPI startup
- `backend/app/env-templates/general-assistant-env/app/workspace/STATUS.md` — placeholder seed file
- `backend/app/env-templates/general-assistant-env/app/workspace/scripts/update_status.py` — helper CLI (`--status`, `--summary`, `--details-file`); atomic writes via `os.replace`
- `backend/app/env-templates/general-assistant-env/app/workspace/knowledge/platform/agents/agent_commands/agent_status_command.md` — synced from `docs/agents/agent_commands/agent_status_command.md`

## Database Schema

Eight columns added to `agent_environment` (all nullable, no indexes — always queried via `environment_id`):

- `status_file_raw` — `TEXT`, last fetched body, capped at 64 KB
- `status_file_severity` — `VARCHAR(16)`, normalized severity (`ok`/`warning`/`error`/`info`/`unknown`)
- `status_file_summary` — `VARCHAR(512)`, parsed summary or first body line
- `status_file_reported_at` — `TIMESTAMP WITH TIME ZONE`, frontmatter timestamp or file mtime
- `status_file_reported_at_source` — `VARCHAR(16)`, `frontmatter` / `file_mtime` / `null`
- `status_file_fetched_at` — `TIMESTAMP WITH TIME ZONE`, when the platform last successfully read the file
- `status_file_prev_severity` — `VARCHAR(16)`, severity before the most recent transition
- `status_file_severity_changed_at` — `TIMESTAMP WITH TIME ZONE`, when the most recent transition occurred

Migration: `backend/app/alembic/versions/34322f866173_add_agent_environment_status_fields.py`. Additive and reversible — downgrade drops all eight columns.

## API Endpoints

- `GET /api/v1/agents/{agent_id}/status?force_refresh=bool` — return current snapshot (cached by default; live fetch with `force_refresh=true`, rate-limited per env at 30 s); `429` on rate-limit, `404` when agent not found, `403` on unauthorized access. Defined in `backend/app/api/routes/agent_status.py:84`.
- `GET /api/v1/agents/status?workspace_id=uuid` — list cached snapshots for every agent the caller can access (cache-only, safe for polling). Defined in `backend/app/api/routes/agent_status.py:54`.
- `POST /api/v1/internal/environments/{env_id}/status-updated` — internal push endpoint; the agent-env watcher calls this when it detects a `STATUS.md` mtime change. Returns `{ok: true, fetched: bool}`. Defined in `backend/app/api/routes/agent_status.py:127`.
- A2A JSON-RPC method `agent/status` — handled in `backend/app/api/routes/a2a.py` (search `agent/status`); returns the same `AgentStatusPublic` shape.

## Services & Key Methods

`backend/app/services/agents/agent_status_service.py` — `AgentStatusService`:
- `fetch_status(environment, db_session=None)` — single round-trip via `adapter.fetch_workspace_item_with_meta()`, parse, persist, transition detection, event emission, activity creation. Raises `StatusUnavailableError` on adapter failure / missing file.
- `parse_status_file(content)` — split optional YAML frontmatter from body; normalize severity; truncate summary; fall back to first non-blank body line when summary missing.
- `get_cached_status(environment)` — build `AgentStatusSnapshot` from persisted row fields without touching the adapter.
- `is_stale(environment)` — true when env not running or `fetched_at` > 10 min old.
- `should_refresh(environment)` — true when snapshot missing or > 5 min old AND not rate-limited (used by the scheduler).
- `is_rate_limited(environment_id)` — module-level dict keyed by env id, 30 s TTL.
- `get_primary_environment(session, agent_id, active_env_id)` — resolves which environment to read (active first, then latest by `updated_at`).
- `empty_snapshot(agent_id)` — sentinel snapshot for agents with no environment.
- `to_public(snapshot, agent_id)` — convert dataclass to `AgentStatusPublic`.

`backend/app/services/agents/commands/agent_status_command.py` — `AgentStatusCommandHandler.execute(context, args)`:
- Fetches live status (falls back to cache on failure) and renders markdown with severity icon, header line, timestamps, divider, body, and an "Environment is not running" notice when serving stale data.

`backend/app/services/environments/environment_status_scheduler.py` — `_check_environment_statuses()`:
- After each healthy env health check, calls `AgentStatusService.fetch_status(env, db_session=session)` when `should_refresh(env)` returns true. Errors swallowed at debug level.

`backend/app/services/environments/adapters/base.py` — `WorkspaceItemMeta` dataclass:
- Fields: `exists`, `size`, `modified_at`, `content_type`. Returned by `fetch_workspace_item_with_meta()`.

`backend/app/services/environments/adapters/docker_adapter.py` — `DockerAdapter.fetch_workspace_item_with_meta()`:
- Single GET to the agent-env file-view endpoint; parses `Last-Modified` / `Content-Length` / `Content-Type` before streaming the body. `download_workspace_item()` is now a thin wrapper that discards the metadata.

## Frontend Components

- `frontend/src/components/Agents/AgentStatusBadge.tsx` — clickable badge in the session header. Reads via `useAgentStatus(agentId)`. Hidden entirely when severity and raw are both null. Opens the dialog on click. Shows a recent-transition chip when `isRecentTransition(status)` is true.
- `frontend/src/components/Agents/AgentStatusDialog.tsx` — shadcn/ui `Dialog` rendering the full markdown body via `MarkdownRenderer`. Footer Refresh button mutates with `force_refresh=true` (swallows `429`); Copy button uses `useCustomToast`. Header strip notes when `reported_at_source === "file_mtime"` and includes the "Changed from `prev_severity`" line on transitions.
- `frontend/src/hooks/useAgentStatus.ts` — `useAgentStatus(agentId, dialogOpen=false)` React Query hook. Query key `["agentStatus", agentId]`; `refetchInterval: 60_000` only when `dialogOpen`; subscribes to `EventTypes.AGENT_STATUS_UPDATED` and invalidates the query on receipt; force-refresh mutation swallows `429`.

## Configuration

- `AgentStatusService.STATUS_FILE_PATH = "STATUS.md"` — relative to workspace root
- `AgentStatusService.MAX_RAW_BYTES = 65536` — 64 KB body cap
- `AgentStatusService.MAX_FRONTMATTER_BYTES = 4096` — 4 KB frontmatter cap
- `AgentStatusService.STALENESS_TTL_SECONDS = 600` — 10 min staleness threshold
- `AgentStatusService.FORCE_REFRESH_TTL_SECONDS = 30` — per-env rate-limit window
- `AgentStatusService.SEVERITY_VALUES = {"ok", "warning", "error", "info"}` — recognized severities (anything else → `unknown`)
- `STATUS_CHECK_INTERVAL_MINUTES = 10` in `environment_status_scheduler.py` — scheduler tick interval

## Security

- **Access control** — REST endpoints use `CurrentUser` and check agent ownership (`agent.owner_id == current_user.id` unless superuser). The slash command inherits session-level auth. The internal push endpoint reuses the agent-env → backend internal auth pattern. A2A `agent/status` is gated by the existing A2A token scope check.
- **Input sanitation** — body capped at 64 KB; summary capped at 512 chars; severity normalized through a closed set; frontmatter > 4 KB falls through to raw-body mode; non-UTF-8 bytes decoded with `errors="replace"`; ISO-8601 timestamps parsed with try/except.
- **No secrets** — `STATUS.md` content is treated as a public artifact: rendered as markdown, returned via API, included in A2A responses, included in activity-feed entries. Documented in `COMPLEX_AGENT_DESIGN.md`.
- **Rate limiting** — `force_refresh` is throttled per environment via in-module dict (mirrors the pattern used by `environment_status_scheduler`). Slash command silently serves cached data when throttled; REST returns `429`.
- **Observability** — `AgentStatusService.fetch_status()` emits structured log lines `agent_status_fetch_success` / `agent_status_fetch_failure` with `agent_id`, `env_id`, severity, transition flag, and failure reason tags (`adapter_error` / `file_missing` / `parse_error` / `rate_limited`).
