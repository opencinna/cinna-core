# Agent Status Tracking — Technical Details

## File Locations

### Backend

**Models**
- `backend/app/models/agents/agent.py` — `AgentBase.status_refresh_command` (`str | None`, default `/run:status`, max 1024 chars); `Agent.status_refresh_command` (`VARCHAR(1024)`, nullable, `server_default='/run:status'`); `AgentUpdate.status_refresh_command`; `AgentPublic.status_refresh_command`. `AgentPublic` also carries three computed boolean fields populated by `AgentService.to_public_with_clone_info`: `has_email_integration`, `has_mcp_connectors`, `has_webhooks` (all default `False`; not stored on the `agent` table — derived at query time).
- `backend/app/models/agents/agent_status.py` — `AgentStatusPublic`, `AgentStatusListPublic` Pydantic response models; `AgentStatusPublic.refresh_command_warning: str | None = None`
- `backend/app/models/environments/environment.py` — `AgentEnvironment` extended with eight `status_file_*` columns
- `backend/app/models/events/event.py` — `EventType.AGENT_STATUS_UPDATED` (outbound WS event) plus the post-action triggers `EventType.CRON_COMPLETED_OK`, `EventType.CRON_TRIGGER_SESSION`, `EventType.CRON_ERROR`

**Services**
- `backend/app/services/agents/agent_service.py` — `AgentService.compute_capability_flags(session, agent_ids) -> dict[UUID, dict[str, bool]]`: one grouped query per capability (`AgentEmailIntegration.enabled`, `MCPConnector.is_active`, `AgentWebhook.enabled`) over the supplied agent-id set; short-circuits to `{}` on an empty list. `AgentService.to_public_with_clone_info(session, agent, capabilities=None)`: accepts a precomputed per-agent flags dict from `compute_capability_flags`; when `None`, computes for the single agent.
- `backend/app/api/routes/agents.py` — `GET /agents/` (`read_agents`) calls `compute_capability_flags` once for the whole page and passes the per-agent slice into each `to_public_with_clone_info` call, keeping the list endpoint off the N+1 path.
- `backend/app/services/agents/agent_status_service.py` — core service; see [Services & Key Methods](#services--key-methods) below for the full method list including the new pre-command helpers
- `backend/app/services/agents/commands/agent_status_command.py` — `AgentStatusCommandHandler` for the `/agent-status` slash command; calls `force_refresh_status(environment, agent=agent, db_session=db)` (the shared force entrypoint), treats an all-`None` snapshot as "no STATUS.md available", and prepends `⚠️ _{warning}_` when a warning is present
- `backend/app/services/agents/commands/__init__.py` — registers the command handler in the command registry
- `backend/app/services/agents/agent_schedule_scheduler.py` — emits `CRON_COMPLETED_OK` / `CRON_TRIGGER_SESSION` / `CRON_ERROR` at every schedule-execution exit point; each event carries `environment_id` in meta
- `backend/app/services/environments/environment_status_scheduler.py` — container health check only (no longer pulls STATUS.md)
- `backend/app/services/environments/adapters/base.py` — `fetch_workspace_item_with_meta()` + `WorkspaceItemMeta` dataclass
- `backend/app/services/environments/adapters/docker_adapter.py` — single-GET implementation that parses `Last-Modified` / `Content-Length` / `Content-Type` headers before streaming the body
- `backend/app/services/sessions/message_service.py` — `MessageService.get_environment_url(environment)` used by `_run_refresh_command` to resolve the container base URL
- `backend/app/services/environments/agent_env_connector.py` — `AgentEnvConnector().exec_command(base_url, auth_token, command, timeout)` — the non-streaming exec path reused by `_run_refresh_command`; raises `RuntimeError` on HTTP error / timeout / connect failure
- `backend/app/main.py` — handler registrations are now derived from `synced_files.SYNCED_FILES` (the Synced Workspace File Registry). For the `"status"` pull-only entry, `AgentStatusService.handle_post_action_event` is registered against `ENVIRONMENT_ACTIVATED`, `STREAM_COMPLETED`, `STREAM_ERROR`, `CRON_COMPLETED_OK`, `CRON_TRIGGER_SESSION`, `CRON_ERROR`, and `WORKSPACE_FILES_CHANGED` (7 events — `ENVIRONMENT_ACTIVATED` is the new addition that closes the "STATUS.md not pulled at activation" gap)

**Routes**
- `backend/app/api/routes/agent_status.py` — public `router` with `GET /agents/status` and `GET /agents/{agent_id}/status`; `force_refresh=True` path is a one-liner call to `AgentStatusService.force_refresh_status(environment, agent=agent)` (the service handles the wake / pre-command / fetch / cache-fallback; the route no longer catches `StatusUnavailableError`)
- `backend/app/api/main.py` — registers `agent_status.router` *before* `agents.router` (so `/agents/status` doesn't get matched as an agent UUID)
- `backend/app/api/routes/a2a.py` — `agent/status` JSON-RPC method handler keeps its own protocol-level `is_rate_limited` pre-check, then calls `force_refresh_status(environment, agent=agent)` on force paths; `refresh_command_warning` is included in the result dict; `status` skill on the agent card

**Migrations**
- `backend/app/alembic/versions/34322f866173_add_agent_environment_status_fields.py` — adds eight `status_file_*` nullable columns to `agent_environment`
- `backend/app/alembic/versions/1a43b403f066_add_agent_status_refresh_command.py` — adds `status_refresh_command VARCHAR(1024)` to `agent` (`server_default='/run:status'`, `nullable=True`); `down_revision=9675dc695735`; downgrade drops the column

**Tests**
- `backend/tests/api/agents/agents_status_test.py` — parser unit tests, timestamp resolution, severity transition, rate-limit, refresh_after_action + handle_post_action_event, REST endpoints
- `backend/tests/stubs/environment_adapter_stub.py` — `EnvironmentTestAdapter.workspace_files` class-level dict + `fetch_workspace_item_with_meta()` stub

### Frontend

**Components**
- `frontend/src/components/Agents/AgentStatusCard.tsx` — Configuration-tab card (owner/developer-only; hidden for `agent-user` role and on foreign installs). Shows the current cached snapshot (severity dot, summary, reported/fetched timestamps). Refresh button triggers a force refresh via `useAgentStatus.forceRefresh`. Editable `status_refresh_command` input (monospace, `maxLength=1024`) persisted via `AgentsService.updateAgent`. Reset-to-default button restores `/run:status` without saving. Backend-authoritative amber warning banner driven by `status.refresh_command_warning`. Hosts `AgentStatusDialog` for full body view. Mounted in `AgentConfigTab` behind the `showOperationalSettings` gate, alongside Schedules and Handovers.
- `frontend/src/components/Agents/AgentStatusCardFooter.tsx` — compact card-footer dot + summary + relative time, click opens the dialog
- `frontend/src/components/Agents/AgentStatusDialog.tsx` — full markdown body + refresh + copy
- `frontend/src/components/Agents/AgentCard.tsx` — hosts the status footer below the main card link; only renders the footer when a status snapshot is available. Also renders a row of capability `Badge` components (API / Web App / Email / MCP / Webhooks / A2A) when any integration flag on `AgentPublic` is truthy, replacing the `entrypoint_prompt` preview in that case. `AgentIntegrationsTab.tsx` and `EmailIntegrationCard.tsx` gained matching Lucide header icons (Waypoints and Mail respectively) for visual consistency with the badge row.
- `frontend/src/components/Agents/AgentConfigTab.tsx` — imports and mounts `AgentStatusCard` in the operational-settings section behind the `showOperationalSettings` gate (alongside Schedules and Handovers)
- `frontend/src/routes/_layout/agents.tsx` — batch-fetches all status snapshots via `listAgentStatuses`, passes each to its `AgentCard`, and subscribes to `EventTypes.AGENT_STATUS_UPDATED` to invalidate the `["agentStatuses"]` query so card footers refresh in real time when the backend publishes a new snapshot

**Hooks & Services**
- `frontend/src/hooks/useAgentStatus.ts` — `useAgentStatus(agentId, dialogOpen)` React Query hook + `severityDotClass` / `severityLabel` / `isRecentTransition` helpers; `forceRefresh` mutation used by both `AgentStatusCard` and `AgentStatusDialog`
- `frontend/src/services/eventService.ts` — `EventTypes.AGENT_STATUS_UPDATED` registered for WS dispatch

**Generated client**
- `frontend/src/client/sdk.gen.ts` — `AgentsService.getAgentStatus({ agentId, forceRefresh? })`, `AgentsService.listAgentStatuses({ workspaceId? })`, `AgentsService.updateAgent({ id, requestBody })` (auto-generated; do not edit)

### Env Template (App Core / Platform Knowledge)

- `backend/app/env-templates/app_core_base/core/prompts/COMPLEX_AGENT_DESIGN.md` — "Agent Self-Reported Status" section + cross-link from "Rules for OK-pattern scripts"
- `backend/app/env-templates/app_core_base/core/main.py` — agent-env FastAPI startup (no status-related wiring — the backend pulls STATUS.md on demand)
- `backend/app/env-templates/platform-knowledge-env/app/workspace/scripts/update_status.py` — helper CLI (`--status`, `--summary`, `--details-file`); writes to `/app/workspace/app-data/storage/STATUS.md` with atomic temp-file + rename. No placeholder file is shipped: `STATUS.md` is per-install state, not bundle content, and the platform creates `app-data/storage/` at install time
- `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/platform/agents/agent_commands/agent_status_command.md` — synced from `docs/agents/agent_commands/agent_status_command.md`

## Database Schema

### `agent` table — new column

One column added to `agent` by migration `1a43b403f066`:

- `status_refresh_command` — `VARCHAR(1024)`, nullable, `server_default='/run:status'`. Existing rows backfill to `/run:status` at migration time. Blank string is treated as deliberate opt-out (no pre-command, no warning).

### `agent_environment` table — snapshot columns

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

- `GET /api/v1/agents/{agent_id}/status?force_refresh=bool` — return current snapshot (cached by default; `force_refresh=true` runs the full force flow via `force_refresh_status`: wake suspended env → pre-command → live fetch → cache fallback). The REST surface does **not** rate-limit force refresh (user-initiated refreshes always fetch; the 30 s per-env limit governs background/event pulls and the A2A force path); `404` when agent not found, `403` on unauthorized access. Defined in `backend/app/api/routes/agent_status.py`.
- `GET /api/v1/agents/status?workspace_id=uuid` — list cached snapshots for every agent the caller can access (cache-only, safe for polling). Defined in `backend/app/api/routes/agent_status.py`.
- A2A JSON-RPC method `agent/status` — handled in `backend/app/api/routes/a2a.py` (search `agent/status`); returns the same `AgentStatusPublic` shape.

## Services & Key Methods

`backend/app/services/agents/agent_status_service.py` — `AgentStatusService`:
- `force_refresh_status(environment, agent=None, db_session=None)` — **the single entrypoint for every user-initiated refresh** (UI Refresh button, REST `?force_refresh=true`, A2A `agent/status` force, `/agent-status` command). Wraps `fetch_status(..., run_refresh_command=True)`; on `StatusUnavailableError` returns `get_cached_status(environment)` with `exc.refresh_command_warning` attached. **Never raises** — always returns an `AgentStatusSnapshot`. Replaced the try/fetch/except-fallback that was previously duplicated across the route, a2a, and command handlers.
- `fetch_status(environment, db_session=None, *, run_refresh_command=False, agent=None)` — single round-trip via `adapter.fetch_workspace_item_with_meta()`, parse, persist, transition detection, event emission, activity creation. Raises `StatusUnavailableError` on adapter failure / missing file. When `run_refresh_command=True`, first calls `_ensure_environment_running(environment, agent)` to wake a suspended env, then `_run_refresh_command(agent, environment)`, attaching any warning to the returned snapshot (or stashing it on `StatusUnavailableError` so callers can surface it on a cached fallback). The adapter is obtained via `EnvironmentService.get_lifecycle_manager().get_adapter(environment)` (not a direct `EnvironmentLifecycleManager()` instantiation — consistent with the rest of the codebase and testable via the lifecycle manager).
- `_ensure_environment_running(environment, agent)` — async; on force paths only, wakes a `suspended` env via `EnvironmentLifecycleManager.activate_suspended_environment()` before the pre-command/fetch. No-op when already running, no agent, or status is anything other than `suspended` (`stopped`/`error` are left to the existing not-running warning). Best-effort — swallows activation errors. Re-reads the env in a fresh session and copies the post-activation `status` + `config` (auth token rotates on activation) back onto the in-memory `environment`; concurrency-safe (picks up a parallel request's wake-up).
- `_run_refresh_command(agent, environment)` — async; resolves and runs the pre-command inside the container. Returns a warning string on any failure, or `None` on success/opt-out. Never raises. Calls `AgentEnvConnector().exec_command(...)` for plain shell commands; resolves `/run:<name>` via `_resolve_run_command`.
- `_resolve_run_command(environment, name)` — static; looks up `name` in `environment.cli_commands_parsed`; returns the resolved shell command string or `None` when not found.
- `_load_agent(agent_id, db_session)` — classmethod; loads an `Agent` row by id, reusing the provided session or opening a new one. Used when `run_refresh_command=True` and no agent was passed.
- `parse_status_file(content)` — split optional YAML frontmatter from body; strict YAML with a lenient line-based fallback for the three known keys; normalize severity; truncate summary; fall back to first non-blank body line when summary missing.
- `get_cached_status(environment)` — build `AgentStatusSnapshot` from persisted row fields without touching the adapter; recomputes `body` by re-parsing the stored `raw`; always returns `refresh_command_warning=None`.
- `refresh_after_action(environment, db_session=None, force=False)` — post-action pull entrypoint. Rate-limit-aware (skipped when `force=True`), swallows `StatusUnavailableError`, logs other errors at debug level. Always calls `fetch_status` with `run_refresh_command=False`.
- `handle_post_action_event(event_data)` — generic event handler. Reads `environment_id` from `event_data["meta"]` and `changed_files` for the `WORKSPACE_FILES_CHANGED` source; loads the env, delegates to `refresh_after_action` with `force=True` when `app-data/storage/STATUS.md` is in `changed_files`, otherwise the speculative (rate-limited) path. Registered against seven events (two session-stream + three CRON + `WORKSPACE_FILES_CHANGED` + `ENVIRONMENT_ACTIVATED`). Never passes `run_refresh_command=True`.
- `is_rate_limited(environment_id)` — module-level dict keyed by env id, 30 s TTL.
- `get_primary_environment(session, agent_id, active_env_id)` — resolves which environment to read (active first, then latest by `updated_at`).
- `empty_snapshot(agent_id)` — sentinel snapshot for agents with no environment; `refresh_command_warning=None`.

`StatusUnavailableError(reason, refresh_command_warning=None)` — carries a transient warning string across failed STATUS.md downloads so callers can attach it to the cached fallback snapshot. `reason` is a plain string tag (`adapter_error`/ `file_missing`); `refresh_command_warning` is the pre-command warning (or `None`).

`backend/app/services/agents/commands/agent_status_command.py` — `AgentStatusCommandHandler.execute(context, args)`:
- Fetches live status (falls back to cache on failure) and renders markdown with severity icon, header line, timestamps, divider, and the parsed `body` (frontmatter stripped). Emits an "Environment is not running" notice when serving cached data from a stopped env.

`backend/app/services/agents/agent_schedule_scheduler.py` — `_emit_cron_event(event_type, schedule, agent, environment_id, **extra_meta)`:
- Best-effort emitter (try/except + `logger.warning`) used at every schedule-execution exit point. Meta always includes `schedule_id`, `schedule_type`, `schedule_name`, `agent_id`, plus `environment_id` whenever the env was resolved.

`backend/app/services/environments/adapters/base.py` — `WorkspaceItemMeta` dataclass:
- Fields: `exists`, `size`, `modified_at`, `content_type`. Returned by `fetch_workspace_item_with_meta()`.

`backend/app/services/environments/adapters/docker_adapter.py` — `DockerAdapter.fetch_workspace_item_with_meta()`:
- Single GET to the agent-env file-view endpoint; parses `Last-Modified` / `Content-Length` / `Content-Type` before streaming the body. `download_workspace_item()` is now a thin wrapper that discards the metadata.

## Frontend Components

- `frontend/src/components/Agents/AgentStatusCard.tsx` — Configuration-tab card (mounted in `AgentConfigTab` behind the `showOperationalSettings` gate; not shown to `agent-user` role or on foreign installs). Props: `agent: AgentPublic`. Uses `useAgentStatus(agent.id)` — `{ status, isLoading, forceRefresh, isRefreshing }`. Local `command` state seeded from `agent.status_refresh_command ?? "/run:status"`, re-seeded via `useEffect` when the prop changes. Save mutation calls `AgentsService.updateAgent({ id, requestBody: { status_refresh_command } })` and invalidates `["agent", agent.id]` + `["agents"]` queries. Reset-to-default button restores `/run:status` in local state (does not save). Warning banner reads `status?.refresh_command_warning` from the server response (backend-authoritative). Hosts `AgentStatusDialog`.
- `frontend/src/components/Agents/AgentStatusCardFooter.tsx` — clickable footer strip inside `AgentCard`. Accepts an `AgentStatusPublic` snapshot as a prop (no per-card fetch), hides itself when severity and raw are both null, opens the dialog on click. Displays severity dot + summary + the agent's own `reported_at` (tooltip shows absolute timestamp). No staleness styling — update cadence is agent-specific.
- `frontend/src/components/Agents/AgentStatusDialog.tsx` — shadcn/ui `Dialog` rendering the parsed body (server-side frontmatter stripped) via `MarkdownRenderer`. Footer Refresh button mutates with `force_refresh=true` (swallows `429`); Copy button uses `useCustomToast` and copies the verbatim `raw`. Header strip shows severity label, summary, reported-at (with file-mtime note), fetched-at, and the "Changed from `prev_severity`" line on recent transitions.
- `frontend/src/hooks/useAgentStatus.ts` — `useAgentStatus(agentId, dialogOpen=false)` React Query hook. Query key `["agentStatus", agentId]`; `refetchInterval: 60_000` only when `dialogOpen`; subscribes to `EventTypes.AGENT_STATUS_UPDATED` and invalidates the query on receipt; force-refresh mutation swallows `429`. Exports `severityDotClass` and `severityLabel` helpers used by both `AgentStatusCardFooter` and `AgentStatusCard`.
- `frontend/src/routes/_layout/agents.tsx` — hosts the batched `["agentStatuses", workspaceId]` query used by `AgentStatusCardFooter`. Subscribes to `EventTypes.AGENT_STATUS_UPDATED` at the page level and invalidates the broad `["agentStatuses"]` key so all visible card footers refresh whenever the backend publishes a new snapshot. Complements the per-agent subscription in `useAgentStatus.ts`, which only covers open dialogs.

## Configuration

- `AgentStatusService.STATUS_FILE_PATH = "app-data/storage/STATUS.md"` — relative to workspace root; lives under the per-install App Data volume so status survives bundle apply-update and is never part of a published bundle revision
- `AgentStatusService.MAX_RAW_BYTES = 65536` — 64 KB body cap
- `AgentStatusService.MAX_FRONTMATTER_BYTES = 4096` — 4 KB frontmatter cap
- `AgentStatusService.FORCE_REFRESH_TTL_SECONDS = 30` — per-env rate-limit window
- `AgentStatusService.STATUS_REFRESH_COMMAND_TIMEOUT = 120` — maximum seconds the pre-command is allowed to run before being treated as a timeout failure
- `AgentStatusService.SEVERITY_VALUES = {"ok", "warning", "error", "info"}` — recognized severities (anything else → `unknown`)
- Frontend constant `DEFAULT_STATUS_REFRESH_COMMAND = "/run:status"` in `AgentStatusCard.tsx` — used as the placeholder text and reset target for the command input

## Security

- **Access control** — REST endpoints use `CurrentUser` and check agent ownership (`agent.owner_id == current_user.id` unless superuser). The slash command inherits session-level auth. A2A `agent/status` is gated by the existing A2A token scope check.
- **Input sanitation** — body capped at 64 KB; summary capped at 512 chars; severity normalized through a closed set; frontmatter > 4 KB falls through to raw-body mode; non-UTF-8 bytes decoded with `errors="replace"`; ISO-8601 timestamps parsed with try/except.
- **No secrets** — `STATUS.md` content is treated as a public artifact: rendered as markdown, returned via API, included in A2A responses, included in activity-feed entries. Documented in `COMPLEX_AGENT_DESIGN.md`.
- **Pre-command warning never includes stdout/stderr** — any failure from `_run_refresh_command` is reported as a generic single-line string. Command output is intentionally excluded from the warning so it cannot leak sensitive data into the public-ish status surfaces (UI banner, API field, A2A response, `/agent-status` chat output).
- **Rate limiting** — `force_refresh` is throttled per environment via in-module dict. Slash command silently serves cached data when throttled; REST returns `429`. The same lock dedupes the post-action handler when it fires within 30 s of another fetch.
- **Observability** — `AgentStatusService.fetch_status()` emits structured log lines `agent_status_fetch_success` / `agent_status_fetch_failure` with `agent_id`, `env_id`, severity, transition flag, and failure reason tags (`adapter_error` / `file_missing` / `parse_error` / `rate_limited`). `_run_refresh_command` emits `agent_status_refresh_command` log lines with `resolved_form` (`run`/`shell`), `outcome` (`ok`/`warning`), and `reason` tags.
