# Cinna CLI Integration — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/cli/__init__.py` — Re-exports all CLI models
- `backend/app/models/cli/cli_setup_token.py` — CLISetupToken (table), CLISetupTokenBase, CLISetupTokenPublic, CLISetupTokenCreate, CLISetupTokenCreated
- `backend/app/models/cli/cli_token.py` — CLIToken (table), CLITokenBase, CLITokenPublic, CLITokenCreate, CLITokenCreated, CLITokensPublic, CLITokenPayload
- `backend/app/models/__init__.py` — Re-exports CLI models at package level

### Backend — Routes

- `backend/app/api/routes/cli.py` — Two routers: `setup_router` (`/api/cli-setup`) and `router` (under `/api/v1/cli`). Contains `_verify_cli_agent_scope()` helper, `get_bootstrap_script()` endpoint, and all agent-scoped CLI endpoints including the sync WebSocket and exec stream

### Backend — Services

- `backend/app/services/cli/cli_service.py` — CLIService: setup token lifecycle, CLI token management, workspace initial clone, building context, knowledge search, sync runtime info, exec streaming, sync WebSocket proxy
- `backend/app/services/cli/cli_auth.py` — CLIAuthService: JWT create/decode, token hashing, WS token extraction
- `backend/app/services/cli/sync_activity_tracker.py` — SyncActivityTracker: register/unregister sync WebSocket connections, heartbeat, grace-period suspend handoff, `is_sync_warm()` gate for the auto-suspend scheduler
- `backend/app/services/cli/cli_setup_token_scheduler.py` — Background scheduler for expired token cleanup (hourly)

### Backend — Dependencies

- `backend/app/api/deps.py` — `CLIContext`, `CLIContextDep`, `get_cli_context()`, `CLIContextWSDep`, `get_cli_context_ws()` (WebSocket variant with rolling-window refresh)

### Backend — App Registration

- `backend/app/main.py` — `setup_router` registered at app level (prefix `/api/cli-setup`); cleanup scheduler started/stopped in lifespan
- `backend/app/api/main.py` — `router` registered under api_router

### Frontend — Components

- `frontend/src/components/Agents/LocalDevCard.tsx` — Setup command display with Regenerate / Copy-token / Copy-command icon buttons, expiry countdown (hidden once expired), active sessions list with per-row sync status indicator, icon-only Disconnect button with Enter-to-confirm dialog
- `frontend/src/components/Agents/LocalDevSyncStatus.tsx` — Small status subcomponent embedded in LocalDevCard rows; shows "Synced" / "Idle" based on `last_sync_connected_at`
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — LocalDevCard added to integrations grid

### Frontend — Generated Client

- `frontend/src/client/sdk.gen.ts` — `CliService` with methods: `createSetupToken`, `listCliTokens`, `revokeCliToken`, `exchangeSetupToken`, `getBuildingContext`, `getWorkspace`, `getSyncRuntime`, `exec`, `searchKnowledge`
- `frontend/src/client/types.gen.ts` — `CLISetupTokenCreated`, `CLITokenPublic`, `CLITokensPublic`, etc.

### Migrations

- `backend/app/alembic/versions/51014db83e57_add_cli_tokens.py` — Creates `cli_setup_token` and `cli_token` tables with indexes
- `backend/app/alembic/versions/c9d0e1f2g3h4_add_env_sync_activity.py` — Adds `last_sync_activity_at` and `sync_active` to `agent_environment`; adds partial index on `sync_active = true`
- `backend/app/alembic/versions/d0e1f2g3h4i5_add_cli_token_last_sync.py` — Adds `last_sync_connected_at` to `cli_token`

### Tests

- `backend/tests/api/cli/test_cli.py` — Scenario tests covering full lifecycle and CLI-authenticated endpoints
- `backend/tests/api/cli/conftest.py` — Patches Docker adapter for test environment
- `backend/tests/utils/cli.py` — Reusable test helpers

## Database Schema

### cli_setup_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| token | VARCHAR(64) | unique, indexed |
| agent_id | UUID | FK -> agent.id, CASCADE |
| environment_id | UUID | FK -> agent_environment.id, SET NULL, nullable |
| owner_id | UUID | FK -> user.id, CASCADE |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | |
| created_at | TIMESTAMP WITH TZ | |

Indexes: `ix_cli_setup_token_token` (unique), `ix_cli_setup_token_owner_agent` (composite)

### cli_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| agent_id | UUID | FK -> agent.id, CASCADE |
| owner_id | UUID | FK -> user.id, CASCADE |
| name | VARCHAR(100) | |
| token_hash | VARCHAR | unique, indexed |
| prefix | VARCHAR(12) | |
| is_revoked | BOOLEAN | default false |
| last_used_at | TIMESTAMP WITH TZ | nullable |
| last_sync_connected_at | TIMESTAMP WITH TZ | nullable |
| machine_info | VARCHAR(200) | nullable |
| expires_at | TIMESTAMP WITH TZ | |
| created_at | TIMESTAMP WITH TZ | |

Indexes: `ix_cli_token_token_hash` (unique), `ix_cli_token_owner_agent` (composite)

### agent_environment (sync-related additions)

| Field | Type | Constraints | Purpose |
|-------|------|-------------|---------|
| last_sync_activity_at | TIMESTAMP WITH TZ | nullable | Updated on sync WS connect and 30s heartbeat; read by auto-suspend scheduler |
| sync_active | BOOLEAN | default false | True while at least one sync WebSocket is connected |

Index: `ix_agent_environment_sync_active` (partial, `WHERE sync_active = true`)

## API Endpoints

### Setup Bootstrap (no auth, under /api)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli-setup/{token}` | Serve bootstrap Python script (checks for `cinna`, delegates or shows install instructions) |
| POST | `/api/cli-setup/{token}` | Exchange setup token for CLI token + bootstrap payload (called by `cinna setup`) |

### CLI Management (user JWT auth)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/cli/setup-tokens` | Generate a setup token for an agent |
| GET | `/api/v1/cli/tokens` | List active CLI tokens (optionally filtered by agent_id) |
| DELETE | `/api/v1/cli/tokens/{token_id}` | Revoke a CLI token |

### Agent-scoped (CLI JWT auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/cli/agents/{agent_id}/workspace` | Download initial workspace tarball (one-shot clone during `cinna setup`) |
| GET | `/api/v1/cli/agents/{agent_id}/building-context` | Get assembled building-mode prompt + settings + inline `prompt_files` dict (contents of `/app/core/prompts/*.md` companions — `WEBAPP_BUILDING.md`, `COMPLEX_AGENT_DESIGN.md`, …) so the CLI can mirror them next to `BUILDING_AGENT.md` |
| POST | `/api/v1/cli/agents/{agent_id}/knowledge/search` | Search agent's knowledge sources |
| WS | `/api/v1/cli/agents/{agent_id}/sync-stream` | Mutagen tunnel WebSocket — proxies raw bytes to env-core `/sync/exec`; registers with SyncActivityTracker; runs 30s heartbeat loop |
| POST | `/api/v1/cli/agents/{agent_id}/exec` | Streaming SSE — body `{command, cwd?}`; first event emits `exec_id`; delegates to env-core `/command/stream` |
| GET | `/api/v1/cli/agents/{agent_id}/sync-runtime` | Returns pinned `{mutagen_version, mutagen_agent_sha256, platform_api_version}` for version verification |

### Env-core callback (bearer + X-Agent-Env-Id header)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/environments/{id}/workspace-files-changed` | Env-core signals one or more watched workspace files (prompts, `CLI_COMMANDS.yaml`, `STATUS.md`) changed. Body: `{"changed_files": [...]}` (optional, informational). Backend emits `WORKSPACE_FILES_CHANGED`; handlers refresh prompts, CLI commands cache, and agent status snapshot. |
| POST | `/api/v1/environments/{id}/prompt-file-changed` | Legacy alias for `workspace-files-changed` — kept so agent environments built before the generic watcher shipped keep working. Emits the same event with no `changed_files` list. |

**Removed endpoints** (were present in previous model, now deleted):

| Method | Path | Reason |
|--------|------|--------|
| GET | `/api/v1/cli/agents/{agent_id}/build-context` | No local container |
| GET | `/api/v1/cli/agents/{agent_id}/credentials` | Credentials stay on the platform |
| POST | `/api/v1/cli/agents/{agent_id}/workspace` | Push superseded by continuous sync |
| GET | `/api/v1/cli/agents/{agent_id}/workspace/manifest` | Diff handled inside Mutagen |

## Services & Key Methods

### CLIService (`backend/app/services/cli/cli_service.py`)

- `create_setup_token()` — Verifies agent ownership, generates random token, stores in DB, returns setup command
- `exchange_setup_token()` — Validates token (not used, not expired), creates CLIToken with JWT, marks setup token as used
- `cleanup_expired_setup_tokens()` — Deletes used tokens >24h old and expired unused tokens
- `list_tokens()` — Returns active (non-revoked, non-expired) tokens for a user, optionally filtered by agent
- `revoke_token()` — Soft-revokes a token (sets `is_revoked=True`), verified by ownership
- `get_workspace_tarball()` — Proxies to env-core HTTP API to download workspace (initial clone only)
- `get_building_context()` — Proxies to env-core prompt generator; falls back to minimal context if env unavailable. The env-core response includes a `prompt_files: {filename: content}` dict with every `.md` under `/app/core/prompts/` except `BUILDING_AGENT.md` (currently `WEBAPP_BUILDING.md`, `COMPLEX_AGENT_DESIGN.md`); the CLI mirrors them next to `BUILDING_AGENT.md` so the on-demand `./<NAME>.md` references in the building prompt resolve locally without a Docker build context
- `search_knowledge()` — Generates query embedding, searches accessible knowledge sources via vector search
- `get_sync_runtime_info()` — Returns pinned Mutagen version + agent binary SHA-256 from `/etc/mutagen-agent.version` inside the env image
- `stream_exec()` — Wraps env-core `/command/stream` via `AgentEnvConnector.stream_command()`; yields chunks as an async generator
- `proxy_sync_stream()` — Accepts client WebSocket, opens env-core `/sync/exec` WebSocket, pumps bytes bidirectionally; handles close/error propagation

### SyncActivityTracker (`backend/app/services/cli/sync_activity_tracker.py`)

- `register_sync_connection(environment_id, token_id)` — Sets `sync_active=true`, `last_sync_activity_at=now`, `last_sync_connected_at=now` on the token; cancels any pending grace-period suspend
- `unregister_sync_connection(environment_id, token_id)` — Decrements in-memory connection count; when it reaches zero sets `sync_active=false` and schedules the grace-period suspend task (default 5 minutes)
- `heartbeat(environment_id)` — Updates `last_sync_activity_at`; called every 30s from an active sync WS
- `is_sync_warm(environment_id)` — Returns `True` when `sync_active=true`; used by the auto-suspend scheduler as a skip gate

### CLIAuthService (`backend/app/services/cli/cli_auth.py`)

- `create_cli_jwt()` — Creates JWT with `sub=token_id`, `agent_id`, `owner_id`, `token_type="cli"`
- `decode_cli_jwt()` — Decodes and validates JWT, checks `token_type=="cli"`
- `decode_cli_jwt_from_ws(websocket)` — Pulls bearer token from `Authorization` header, subprotocol, or `?token=` query (in priority order) for WebSocket routes
- `hash_token()` — SHA-256 hash for secure storage

### get_cli_context / get_cli_context_ws (`backend/app/api/deps.py`)

- Decodes CLI JWT and verifies `token_type`
- Looks up CLIToken by ID, checks revocation and expiry
- Loads agent, verifies ownership match
- Loads active environment for the agent
- Updates `last_used_at` and renews `expires_at` (rolling 7-day window)
- `get_cli_context_ws` additionally handles WebSocket token extraction and rolling-window refresh on the WS handshake

## CLI-side Layout

> The `cinna-cli` code lives in a separate repository. Files below are for orientation only; consult that repo for current source. <!-- nocheck -->

- `src/cinna/main.py` — `click` command group: `setup`, `exec`, `status`, `dev`, `sync` (with `status` / `conflicts` subcommands), `list`, `disconnect`, `disconnect-all`, `mcp-proxy` (hidden) <!-- nocheck -->
- `src/cinna/bootstrap.py` — `cinna setup` flow: token exchange, Mutagen install check, workspace clone, `CLAUDE.md` / `BUILDING_AGENT.md` / companion prompt-file materialisation, sync session start, foreground TUI attach <!-- nocheck -->
- `src/cinna/sync_session.py` — wraps `mutagen sync create/list/terminate/flush`; stores session state and parses Mutagen's `--template '{{json .}}'` output (Mutagen 0.18.1 has no `--json` flag) <!-- nocheck -->
- `src/cinna/sync_tui.py` — Textual two-tab TUI (Tab 1 = friendly status + activity log, Tab 2 = raw `mutagen sync list --long`); Ctrl-C / `q` terminate the session on exit <!-- nocheck -->
- `src/cinna/sync_ssh_shim.py` — `cinna-sync-ssh` binary that Mutagen invokes as an SSH transport. Parses the fake `cinna-agent-<uuid>` host, looks up credentials in `~/.cinna/agents.json` (registry wins over env vars so rotated tokens propagate without restarting the Mutagen daemon), opens a WebSocket to `/api/v1/cli/agents/{id}/sync-stream`, pumps stdin/stdout/stderr <!-- nocheck -->
- `src/cinna/context.py` — generates `CLAUDE.md` and `BUILDING_AGENT.md`, mirrors companion `/app/core/prompts/*.md` files referenced by the building prompt: prefers the inline `prompt_files` dict from the building-context response, falls back to legacy `.cinna/build/app/core/prompts/` if the dict is absent <!-- nocheck -->
- `src/cinna/config.py` — `CinnaConfig` dataclass (per-workspace `.cinna/config.json`) + global `~/.cinna/agents.json` registry helpers (`upsert_agent_registry`, `remove_agent_registry`, `list_agent_registry`, `lookup_agent_registry`) <!-- nocheck -->

## Agent Registry (`~/.cinna/agents.json`)

Per-user JSON file mapping `agent_id` → `{platform_url, cli_token, frontend_url, workspace_path}`. Written in file-permission mode `0o600`. Two primary consumers:

1. **`cinna-sync-ssh` shim** — Mutagen spawns the shim for every SSH connection it opens. A single shared Mutagen daemon may serve multiple agents, and the daemon captures env vars once at start — so per-agent credentials must come from a fresh source on each invocation. The shim resolves them by `agent_id` from the registry.
2. **`cinna list`** — lists every registered agent with UI link (built from `frontend_url`), workspace path, and live sync state (looked up once via `mutagen sync list --template '{{json .}}'` and indexed by session name).

Entries are upserted on every `cinna setup` (refreshing the token) and removed by `cinna disconnect` / `cinna disconnect-all`.

## Env-Core Additions

### WS /sync/exec (inside the container)

- File: `backend/app/env-templates/app_core_base/core/server/routes.py`
- Internal-only — not publicly routed; reachable only from the backend's internal Docker network
- Accepts WebSocket handshake; reads a JSON preamble frame describing the `mutagen-agent` invocation args
- Spawns `mutagen-agent <args>` with `cwd = WORKSPACE_ROOT`
- Three concurrent tasks: WS → stdin pump, stdout → WS pump, stderr → container log
- On WS close or process exit: SIGTERM the subprocess, SIGKILL after 2s grace, close WS cleanly

### GET /prompt/building (building context assembly)

- File: `backend/app/env-templates/app_core_base/core/server/routes.py`
- Returns `{building_prompt, building_prompt_parts, prompt_files, settings}`
- `building_prompt` — fully assembled building-mode system prompt (built by `PromptGenerator.generate_building_mode_prompt()`); still contains `/app/core/prompts/<NAME>.md` references that the CLI rewrites to `./<NAME>.md` after materialising the companions
- `building_prompt_parts` — individual raw parts (BUILDING_AGENT.md body, scripts README, workflow prompt, entrypoint prompt, refiner prompt, credentials README, knowledge topics, handover config) used for diffing in future tooling
- `prompt_files` — dict `{filename: content}` for every `.md` file under `/app/core/prompts/` except `BUILDING_AGENT.md` itself. Shipped inline so the CLI can write them next to `BUILDING_AGENT.md` at the workspace root — this replaces the legacy behaviour of extracting them from the Docker build context tarball (no longer produced in live-sync mode)

### Workspace files watcher

- Lightweight mtime-poll watcher in `core/main.py` (`_workspace_files_watcher`) monitors `docs/WORKFLOW_PROMPT.md`, `docs/ENTRYPOINT_PROMPT.md`, `docs/REFINER_PROMPT.md`, `docs/CLI_COMMANDS.yaml`, `docs/STATUS.md` under `WORKSPACE_ROOT` <!-- nocheck -->
- Polls every 5 s; fires when a file is stable for at least one polling interval after a change (debounces Mutagen transfer bursts)
- POSTs the list of changed paths to `POST /api/v1/environments/{id}/workspace-files-changed` (bearer auth + `X-Agent-Env-Id` header)
- Route (`backend/app/api/routes/environments.py`) parses the optional `WorkspaceFilesChangedRequest` body and delegates to `EnvironmentService.emit_workspace_files_changed()`; the legacy `prompt-file-changed` endpoint is a thin alias that calls the same service method with `changed_files=None`
- `EnvironmentService.emit_workspace_files_changed()` (`backend/app/services/environments/environment_service.py`) looks up the agent (raises `AgentNotFoundError` if missing) and emits `EventType.WORKSPACE_FILES_CHANGED` with `environment_id`, `agent_id`, and optional `changed_files` in meta
- Event subscribers (registered in `backend/app/main.py`): `EnvironmentService.handle_workspace_files_changed_event` (prompt resync), `CLICommandsService.handle_post_action_event` (CLI_COMMANDS.yaml cache), `AgentStatusService.handle_post_action_event` (STATUS.md snapshot)

### Docker image

- `mutagen-agent` binary baked into the agent env Dockerfile at the pinned `CLI_MUTAGEN_VERSION` build arg
- Installed at `/usr/local/bin/mutagen-agent`; version + SHA-256 recorded at `/etc/mutagen-agent.version`

## Frontend Components

### LocalDevCard (`frontend/src/components/Agents/LocalDevCard.tsx`)

- **Setup button** — Triggers `CliService.createSetupToken`, displays curl command
- **Command display** — Readonly input with three inline icon buttons: Regenerate (refresh), Copy Token (key icon — copies raw `setupToken.token`), Copy Command (clipboard — copies the full `setup_command`); each copy shows a 2s green-check confirmation via `copiedId` state
- **Expiry countdown** — `useEffect` + `setInterval`, renders "Expires in Xm Ys" while `secondsLeft > 0`; hidden once expired
- **Active sessions list** — `useQuery` with key `["cli-tokens", agentId]`, shows machine_info/name/prefix + sync status indicator (via `LocalDevSyncStatus`)
- **Disconnect control** — Icon-only destructive button (`Unplug` lucide icon) with tooltip "Disconnect"; opens an AlertDialog that uses `onOpenAutoFocus={(e) => e.preventDefault()}` plus `autoFocus` on the destructive `AlertDialogAction` so Enter triggers disconnect (Escape cancels); calls `revokeCliToken` and invalidates the query on success

### LocalDevSyncStatus (`frontend/src/components/Agents/LocalDevSyncStatus.tsx`)

- Embedded in each session row
- Reads `last_sync_connected_at` from the `["cli-tokens", agentId]` query (no additional query)
- Shows "Synced <relative time>" (green dot) when `last_sync_connected_at` is recent (<5min); "Idle <relative time>" (gray) otherwise

## Sync Lifecycle State Machine

```
suspended ──(WS connect)──▶ running + sync_active=true
                                │
                                ├─(WS disconnect, last one)──▶ running + sync_active=false + grace timer
                                │                                          │
                                │                                          ├─(new WS within grace)──▶ running + sync_active=true
                                │                                          │
                                │                                          └─(grace elapsed, no other activity)──▶ normal auto-suspend path
                                │
                                └─(30s heartbeat)──▶ updates last_sync_activity_at
```

## Security

- Setup tokens: 15-minute TTL, single-use, token value is a 32-char URL-safe random string
- CLI tokens: JWT with HS256, 7-day rolling expiry, hash-stored in DB
- Token value shown only once at creation (same pattern as A2A access tokens)
- Every CLI API call validates: JWT signature, DB lookup, revocation check, ownership check
- Agent scope enforced per-endpoint via `_verify_cli_agent_scope()` helper
- Credentials never written to the user's machine
- Sync WebSocket connection rate-limited per token (guard against reconnect loops)
- Exec endpoint inherits rate limits from the in-session `/run:*` path
- Env-core `/sync/exec` is only reachable on the internal Docker network; never publicly routed

## Reverse Proxy Requirements

The sync WebSocket (`WS /api/v1/cli/agents/{agent_id}/sync-stream`) and the SSE exec stream (`POST /api/v1/cli/agents/{agent_id}/exec`) both need a reverse-proxy location that enables WebSocket upgrade headers, disables buffering, and sets a long read/send timeout (so idle Mutagen tunnels and streaming exec output aren't severed). The `frontend/nginx.conf` ships a dedicated `location /api/v1/cli/` block; the production reverse proxy needs the same.

See [Nginx Setup — `/api/v1/cli/`](../../infrastructure/nginx_setup.md#apiv1cli) for the exact directives and rationale.
