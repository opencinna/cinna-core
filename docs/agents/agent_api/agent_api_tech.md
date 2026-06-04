# Agent REST API — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/agent_api/agent_api_token.py` — `AgentApiToken` (table, with `credential_id` FK), `AgentApiTokenBase`, `AgentApiTokenCreate`, `AgentApiTokenPublic`, `AgentApiTokenCreated`, `AgentApiConnectedAgent`, `AgentApiConnectionInfo`, `AgentApiProducerConnection`, `AgentApiProducerConnections`, `ConnectAgentApiRequest`, `ConnectAgentApiResponse` (no manual token-CRUD models — `AgentApiTokenUpdate`/`AgentApiTokensPublic` were removed)
- `backend/app/models/agents/agent.py` — `agent_api_enabled: bool` field on `Agent` (table), `AgentUpdate`, and `AgentPublic`
- `backend/app/models/environments/environment.py` — `agent_api_spec_parsed`, `agent_api_spec_fetched_at`, `agent_api_spec_error`, `agent_api_policy_cache` cache columns on `AgentEnvironment`
- `backend/app/models/credentials/credential.py` — `AGENT_API` added to `CredentialType` enum
- `backend/app/models/__init__.py` — re-exports all agent_api models

### Backend — Routes

- `backend/app/api/routes/agent_api.py` — owner-preview routes + connect helper + producer connections list/delete; prefix `/api/v1/agents/{agent_id}/agent-api`
- `backend/app/api/routes/agent_api_public.py` — consumer serving routes (token auth); prefix `/api/v1/agent-api/{agent_id}`
- `backend/app/api/routes/credentials.py` — `GET /credentials/{id}/agent-api-connection` (connection detail for the credential page)
- `backend/app/api/main.py` — both agent_api routers registered

### Backend — Services

- `backend/app/services/agent_api/agent_api_service.py` — `AgentApiService` + exception hierarchy
- `backend/app/services/agent_api/agent_api_token_service.py` — `AgentApiTokenService`
- `backend/app/services/environments/adapters/docker_adapter.py` — `get_agent_api_status()`, `get_agent_api_spec()`, `proxy_agent_api()`
- `backend/app/services/environments/environment_lifecycle.py` — `agent_api/` added to `dirs_to_copy`
- `backend/app/services/credentials/credentials_service.py` — `AGENT_ENV_ALLOWED_FIELDS["agent_api"]` + `SENSITIVE_FIELDS["agent_api"]` + `_rewrite_agent_api_urls_for_env()` (swaps the stored public URL host for `AGENT_ENV_BACKEND_URL` on env sync)
- `backend/app/core/config.py` — `AGENT_ENV_BACKEND_URL` (container-reachable backend origin; also the env's `BACKEND_URL`)

### Backend — Migrations

| Revision ID | File | What it does |
|-------------|------|-------------|
| `aa11agentapi01` | `aa11agentapi01_add_agent_api_enabled_to_agent.py` | Adds `agent_api_enabled BOOL NOT NULL DEFAULT false` to `agent` |
| `aa22agentapi02` | `aa22agentapi02_add_agent_api_cache_to_environment.py` | Adds `agent_api_spec_parsed` (JSON), `agent_api_spec_fetched_at`, `agent_api_spec_error` (VARCHAR 512), `agent_api_policy_cache` (JSON) to `agent_environment` |
| `aa33agentapi03` | `aa33agentapi03_add_agent_api_token_table.py` | Creates `agent_api_token` table, including the `credential_id` FK (`ON DELETE CASCADE`) + its index (see schema below) |
| `aa44agentapi04` | `aa44agentapi04_add_agent_api_to_credential_type_enum.py` | `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'AGENT_API'` (non-transactional, mirrors prior enum migrations) |

| `c7e2a9f4b1d8` | `c7e2a9f4b1d8_backfill_agent_api_credential_workspace.py` | **Data-only backfill** for Automatic Credentials grouping. Sets `user_workspace_id` on legacy `agent_api` credentials where it is currently `NULL` and the credential is linked to exactly one agent that carries a non-null workspace. Leaves `NULL` when the link is ambiguous (zero links, multiple links, or linked agent is in the default workspace). Downgrade is a no-op. Credentials that were already `NULL` and stay `NULL` appear in the Automatic Credentials section only under the default-workspace / unfiltered view — correct grouping, no data loss. |

Chain: `aa11 → aa22 → aa33 → aa44`. These migrations were authored together and never released; `aa33` was edited in place to carry `credential_id` (no separate migration). `c7e2a9f4b1d8` is a later, independent data-only migration. A fresh `alembic upgrade head` yields the current schema.

### Env-Core (inside container)

- `backend/app/env-templates/app_core_base/core/cinna_api/__init__.py` — SDK public surface: `api`, `credentials`, `error`, ergonomic re-exports
- `backend/app/env-templates/app_core_base/core/cinna_api/credentials.py` — fresh-read `credentials.json` accessor
- `backend/app/env-templates/app_core_base/core/cinna_api/errors.py` — `error()` structured-error helper
- `backend/app/env-templates/app_core_base/core/cinna_api/supervisor.py` — `AgentApiSupervisor` class + `agent_api_supervisor` singleton
- `backend/app/env-templates/app_core_base/core/cinna_api/discovery.py` — module discovery and FastAPI app construction
- `backend/app/env-templates/app_core_base/core/cinna_api/harvest.py` — import-only spec harvest subprocess entry point (`python -m core.cinna_api.harvest`)
- `backend/app/env-templates/app_core_base/core/cinna_api/serve.py` — `app = FastAPI()` entry point for the uvicorn child
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `GET /agent-api/_status`, `GET /agent-api/openapi.json`, `ANY /agent-api/proxy/{path}`, `POST /environments/{id}/agent-api-reloaded`
- `backend/app/env-templates/app_core_base/core/scripts/scaffold_agent_api.py` — scaffolder that writes a working `orders.py` (incl. `API_TITLE`/`API_DESCRIPTION`/`API_VERSION` example constants) + `policy.yaml` stub
- `backend/app/env-templates/app_core_base/core/prompts/REST_API_BUILDING.md` — the building guide injected into the producer agent (SDK usage, spec-naming constants, policy, stray-file pitfall, scaffolding)

### Frontend

- `frontend/src/components/Agents/AgentRestApiCard.tsx` — producer "Agent REST API" card: enable toggle, error banner (compact summary + Details toggle + Retry), a View Spec + Refresh button row (View Spec opens the spec viewer tab via `openAgentApiSpec()`; Refresh calls `refreshAgentApiStatus` to re-harvest the spec + re-parse policy on demand), and the Connections list (consumer Bot badges + Disconnect). Hosted by `AgentIntegrationsTab.tsx`. Module helpers `parseHttpStatus()` / `summarizeBootError()` turn a raw `last_error` into the one-line summary.
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — renders `AgentRestApiCard`
- `frontend/src/components/Credentials/AgentApiConnectionView.tsx` — `agent_api` credential detail panel: producer + connected agents (Bot badges) + View Spec (opens the producer's spec viewer tab) + editable name/notes
- `frontend/src/routes/agent-api-spec/$agentId.tsx`, `frontend/src/components/Agents/OpenApiSpecViewer.tsx`, `frontend/src/utils/agentApiSpec.ts` — the rendered spec viewer (route + renderer + launch helper). See [spec_viewer_tech.md](spec_viewer_tech.md).
- `frontend/src/components/Credentials/ConnectAgentApiDialog.tsx` — "Connect Agent API"; thin wrapper over the shared `AgentSelectorDialog`, filtered to API-enabled agents
- `frontend/src/components/Common/AgentSelectorDialog.tsx` — shared agent picker (Bot badge + colour preset) reused by the connect dialog
- `frontend/src/components/Credentials/AddCredential.tsx` — "Connect Agent API" entry point in the add-credential picker
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` — per-agent "Connect Agent API" button (passes the consumer agent id)
- `frontend/src/components/Credentials/credentialTypes.ts` — `agent_api` display-only type meta (icon/label/badge); not offered in the manual picker
- `frontend/src/routes/_layout/credential/$credentialId.tsx` — `agent_api` branch renders three stacked cards: (1) **Basic Information** card — editable `name` and `notes` fields only, wired to `onSubmitMetadataOnly` which calls `updateCredential` with `{name, notes}` (never the proxy token); (2) `AgentApiConnectionView` — read-only connection panel (producer/consumers/View Spec); (3) `CredentialSharing`. `CredentialTemplateSharing` is **not rendered** for `agent_api` credentials — template sharing is meaningless for a connection credential that has no user-fillable private fields. Note: `AgentApiConnectionView.tsx` itself also contains an embedded name/notes form used when the connection view is rendered in contexts other than the owned credential detail page; on the detail route the `$credentialId.tsx`-level Basic Information card takes precedence.
- `frontend/src/routes/_layout/credentials.tsx` — partitions the single `["credentials", workspaceFilter]` query result into two sections: **My Credentials** (`type !== "agent_api"`) and **Automatic Credentials** (`type === "agent_api"`). The Automatic Credentials section is hidden when empty; when visible it shows the same `CredentialGrid` component with a one-line explainer ("Connections created by 'Connect Agent API'. Manage name, notes, and sharing here."). Workspace filter applies automatically because the query is shared and agent_api credentials are now workspace-stamped at connect time.
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` — `agent_api_enabled` toggle
- `frontend/src/components/Environment/EnvironmentPanel.tsx` — "Agent API" tab in the workspace file tree

### Tests

- `backend/tests/api/agents/agents_agent_api_test.py` — 31 scenario-based API tests (see test coverage section below)

---

## Database Schema

### `agent` table (modified)

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `agent_api_enabled` | `BOOL NOT NULL` | `false` | Whether the Agent REST API feature is active for this agent |

### `agent_environment` table (modified)

| Column | Type | Description |
|--------|------|-------------|
| `agent_api_spec_parsed` | `JSON` (nullable) | Cached harvested OpenAPI spec dict; populated on reload notification or on-demand harvest |
| `agent_api_spec_fetched_at` | `TIMESTAMPTZ` (nullable) | When the spec cache was last refreshed |
| `agent_api_spec_error` | `VARCHAR(512)` (nullable) | Last harvest/boot error message; coexists with a previously good spec |
| `agent_api_policy_cache` | `JSON` (nullable) | Parsed `policy.yaml` dict; uses `DEFAULT_POLICY` when absent; `FAIL_CLOSED_POLICY` on parse error |

### `agent_api_token` table (new)

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID PK` | |
| `agent_id` | `UUID FK → agent` | Producer agent; `CASCADE` delete |
| `owner_id` | `UUID FK → user` | `CASCADE` delete |
| `credential_id` | `UUID FK → credential` (nullable) | The connection credential this token backs; `ON DELETE CASCADE` (deleting the credential = disconnect). Nullable only transiently during connect, and for legacy orphan tokens. |
| `token_hash` | `VARCHAR(64)` | SHA256 hex digest of the opaque token; unique indexed |
| `token_prefix` | `VARCHAR(12)` | First 8 chars of the token value, for display |
| `label` | `VARCHAR(255)` (nullable) | Connection label |
| `read_only_override` | `BOOL NOT NULL` | `true` forces GET/HEAD-only; may only narrow the policy, never widen |
| `is_active` | `BOOL NOT NULL` | Active flag (kept for validation; revocation is by deleting the credential) |
| `last_used_at` | `DATETIME` (nullable) | Bumped on each successful `validate_token()` call |
| `created_at` | `DATETIME NOT NULL` | |
| `updated_at` | `DATETIME NOT NULL` | |

Indexes: unique on `token_hash`; btree on `agent_id`; btree on `credential_id`.

### `credential` table (enum modified)

`CredentialType` PostgreSQL native enum extended with `'AGENT_API'`. The `credential_data` (encrypted) shape for this type: `{ base_url, token, spec_url, label, producer_agent_id }`.

---

## Credential Pipeline

### `AGENT_ENV_ALLOWED_FIELDS["agent_api"]`

`["base_url", "spec_url", "token", "label", "producer_agent_id"]` — all five fields are synced into `credentials.json` in the consumer's container. The consumer's code needs `token` to authenticate and `base_url` to call the proxy.

### `SENSITIVE_FIELDS["agent_api"]`

`["token"]` — the proxy token is redacted (`***REDACTED***`) when the platform writes `README.md` / credential summaries injected into the agent's building prompt. `base_url` and `spec_url` are shown in clear (they are safe to display; the consumer needs to know where to call).

### URL rewrite on env sync (`_rewrite_agent_api_urls_for_env`)

The credential **stores** the public proxy URL (`{FRONTEND_HOST}/api/v1/agent-api/{id}`, built by `AgentApiTokenService.build_base_url`) so the UI shows a human-clickable address. But a consumer agent reads `base_url` from `credentials.json` and calls it **from inside its Docker container**, where the public host is not the backend — `http://localhost:5173` (local dev) resolves to the container itself, and the public domain may not be routable from the agent network.

`prepare_credentials_for_environment` therefore rewrites `agent_api` `base_url`/`spec_url` **before filtering**: it swaps only the scheme+host (netloc) to `settings.AGENT_ENV_BACKEND_URL` (the same value injected as `BACKEND_URL` into the env's `.env`), preserving the `/api/v1/agent-api/{id}[/openapi.json]` path. The rewrite happens only on the env-synced copy (and the matching README) — the stored credential and the credential detail UI keep the public URL. Both write paths (initial env creation in `environment_lifecycle.py` and `sync_credentials_to_agent_environments`) flow through `prepare_credentials_for_environment`, so every `credentials.json` write is covered.

`AGENT_ENV_BACKEND_URL` (config.py, default `http://backend:8000`) is the single source of truth for the container-reachable backend origin, used both here and as the env's `BACKEND_URL`.

---

## API Routes

### Owner Preview (`backend/app/api/routes/agent_api.py`)

Prefix: `/api/v1/agents/{agent_id}/agent-api`; requires authenticated owner (or superuser).

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/_status` | Build/run status. Never spawns the serving child. Works regardless of `agent_api_enabled` (reports `disabled`). |
| `POST` | `/_refresh` | Force an import-only re-harvest (`get_spec(force_refresh=True)` + `load_policy(force_refresh=True)`) and return the updated status. Refreshes the cached spec **and** re-parses `policy.yaml`, and clears a sticky boot/harvest error that would otherwise only clear on the next automatic re-harvest. Best-effort — never raises on a harvest failure; the returned `last_error` reflects the outcome. Drives the producer card's **Refresh** and **Retry** buttons. |
| `GET` | `/openapi.json` | Harvested spec from cache, or import-only harvest. Requires `agent_api_enabled`. |
| `ANY` | `/proxy/{path:path}` | Full HTTP passthrough for owner testing. Requires running env. No policy enforcement (owner-only). Excluded from OpenAPI schema. |
| `POST` | `/connect` | "Connect Agent API" helper — mints token + creates connection credential + optional consumer link. Returns `ConnectAgentApiResponse`. |
| `GET` | `/connections` | List the connections (consumers) of this producer. Returns `AgentApiProducerConnections`. |
| `DELETE` | `/connections/{token_id}` | Disconnect — deletes the connection credential (cascade-deletes the token) or an orphaned token directly. |

There are **no** token-CRUD routes; tokens are created only via `/connect` and removed only via `/connections/{token_id}` (or by deleting the credential).

### Credential Connection Detail (`backend/app/api/routes/credentials.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/credentials/{id}/agent-api-connection` | Connection detail for an `agent_api` credential: producer agent (+name), `base_url`/`spec_url`, `read_only`, and linked consumer agents (with `ui_color_preset`). Returns `AgentApiConnectionInfo`. Owner-only (404 on non-owner). |

### Consumer Serving (`backend/app/api/routes/agent_api_public.py`)

Prefix: `/api/v1/agent-api/{agent_id}`; token auth via `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/openapi.json` | Spec passthrough; subject to `expose_spec` in policy. `403` when `expose_spec=false`. |
| `ANY` | `/{path:path}` | Full HTTP passthrough: validate token → enforce policy → compute request-loop headers → keep-alive → auto-activate producer env → adapter proxy. Excluded from OpenAPI schema. |

Agent disabled or not found → `404` (no existence leak). Invalid/revoked token → `401`.

---

## Services and Key Methods

### `AgentApiService` (`backend/app/services/agent_api/agent_api_service.py`)

**Environment resolution:**
- `resolve_producer_environment(session, agent_id, user_id, is_superuser, require_agent_api_enabled)` — delegates to `WebappService.resolve_agent_environment()` verbatim (same env-selection rule, stable across blue-green swap/rebuild), then re-checks `agent_api_enabled`. Raises `AgentApiNotFoundError` (404), `AgentApiDisabledError` (400), `AgentApiNotRunningError` (503).
- `resolve_agent_only(session, agent_id, user_id, is_superuser)` — resolves + ownership-checks the agent without requiring a running env. Used by `_status`.
- `resolve_running_producer_env(session, agent)` — resolves on behalf of the producer owner. **Fast path:** env running → return it. **Cold path:** env suspended/stopped/mid-activation → kick off activation and **block up to `ACTIVATION_WAIT_SECONDS` (10s)** for the container to come up, then return the now-running env so the call is forwarded (the consumer's first call after idle just takes a little longer; subsequent calls hit the fast path). Raises `AgentApiNotRunningError` (503) only when activation errors or the env is still not running after the grace window (consumer retries — by then the env is typically up).
- `_wait_for_running_env(session, environment_id)` — polls the env status every `ACTIVATION_POLL_INTERVAL` (0.5s) until `running`. Each poll reads through a **short-lived `create_session()`** (fresh session per iteration) — this releases the connection before each `asyncio.sleep` (so concurrent cold starts can't pin/starve the pool) and observes the background activation task's commits (it runs in its own session). Mirrors `SessionService._wait_for_environment_ready`. Status is checked once *before* the first sleep, so an already-ready env returns with no polling delay; on success the env is re-read + `refresh`ed into the request `session` for the caller. Raises `AgentApiNotRunningError` (503) on `error` status or when the 10s budget elapses.
- `authorize_consumer_request(session, agent, token, method, path, body_size, incoming_headers)` → `(environment, hop_headers)` — orchestrates: load cached policy → enforce it → compute request-loop headers → resolve + auto-activate-and-wait for env. Policy enforcement runs BEFORE env resolution so a 405/413/429 never wakes a suspended env.

**Status and spec:**
- `get_status(session, agent, environment)` → `dict` — reports `state` of `disabled` | `not_running` | `running` | `error`, spec availability, last error, policy summary. Never spawns the serving child.
- `get_spec(session, environment, force_refresh)` → `dict` — serves from `agent_api_spec_parsed` cache when present; otherwise calls `adapter.get_agent_api_spec()` (import-only harvest via env-core, no child spawn). Caches result. Raises if env not running and cache is cold.
- `cache_spec(session, environment, spec)` — persists spec + clears prior error.
- `refresh_spec_cache(environment_id)` — async class method called on the env-core reload notification; re-harvests + re-caches spec + policy; emits `AGENT_API_STATUS_CHANGED` event. Best-effort, never raises.

**Keep-alive and activation:**
- `update_last_activity(session, environment)` — bumps `last_activity_at` so the suspension scheduler respects active API consumers, exactly like the webapp.
- `auto_activate_if_wakeable(session, agent, environment)` → `dict` — kicks off activation for an env in any `WAKEABLE_ENV_STATUSES` (`"suspended"`, `"stopped"`) via `EnvironmentService.activate_environment()` (which internally starts the container for both cases); returns `{status: activating|running|error, message}`. `creating`/`starting`/`activating` report `activating` without re-kicking; any other status reports `error`.

**Policy:**
- `load_policy(session, environment, force_refresh)` → `dict` — fetches `agent_api/policy.yaml` via the adapter, parses it, caches on `agent_api_policy_cache`. Missing file → `DEFAULT_POLICY`. Parse error → `FAIL_CLOSED_POLICY` (deny-all).
- `get_effective_policy(session, agent)` → `dict` — returns `agent_api_policy_cache` from the active env row, or `DEFAULT_POLICY` if not yet cached.
- `enforce_policy(policy, method, path, body_size, token, hop_depth)` — raises `AgentApiPolicyError` (405/413/429/403). Token `read_only_override` may only narrow. Rate limit is per-token, with the limit taken from the producer's `policy.yaml`.
- `parse_policy(raw)` — pure function; merges parsed YAML over `DEFAULT_POLICY`; `FAIL_CLOSED_POLICY` on error.

**Request-loop protection:**
- `next_hop_headers(incoming_headers)` → `dict` — computes the `x-cinna-agent-api-deadline-ms` (shrinks by `HOP_DEADLINE_SHRINK_MS=1000ms` per hop) and `x-cinna-agent-api-hop-depth` (increments) headers to forward downstream. Raises `AgentApiPolicyError` (403) if budget exhausted or depth exceeds `MAX_HOP_DEPTH=4`.
- `incoming_hop_depth(incoming_headers)` → `int` — extracts current depth from headers.

**Exception hierarchy:**
- `AgentApiError(message, status_code)` — base
- `AgentApiNotFoundError` → 404
- `AgentApiDisabledError` → 400
- `AgentApiNotRunningError` → 503
- `AgentApiAuthError` → 401
- `AgentApiPolicyError(message, status_code, retry_after)` — 405/413/429/403

**Constants:**
- `DEADLINE_HEADER = "x-cinna-agent-api-deadline-ms"`
- `HOP_DEPTH_HEADER = "x-cinna-agent-api-hop-depth"`
- `MAX_HOP_DEPTH = 4`
- `DEFAULT_DEADLINE_MS = 60_000`
- `HOP_DEADLINE_SHRINK_MS = 1_000`
- `ACTIVATION_WAIT_SECONDS = 10.0` — cold-start grace window: how long a consumer request blocks waiting for a suspended/stopped producer env to come up before returning 503
- `ACTIVATION_POLL_INTERVAL = 0.5` — env-status re-check cadence during the cold-start wait
- `WAKEABLE_ENV_STATUSES = ("suspended", "stopped")` — env statuses a consumer request will auto-activate
- `DEFAULT_POLICY` — `{read_only: true, auth: required, max_body_bytes: 10MB, rate_limit: "60/min", expose_spec: true, allowed_paths: ["*"]}`
- `FAIL_CLOSED_POLICY` — deny-all (`allowed_methods: []`, `rate_limit: "0/min"`, `expose_spec: false`, `allowed_paths: []`)

### `AgentApiTokenService` (`backend/app/services/agent_api/agent_api_token_service.py`)

- `build_base_url(agent_id)` → absolute consumer-facing proxy URL (`{FRONTEND_HOST}/api/v1/agent-api/{agent_id}`)
- `build_spec_url(agent_id)` → `{base_url}/openapi.json`
- `create_token(session, agent_id, user_id, data, is_superuser)` → `AgentApiTokenCreated` — internal mint: `secrets.token_urlsafe(32)` value, SHA256 hash, 8-char prefix; never expires. Called only by `connect_agent_api` (not exposed via a route).
- `validate_token(session, agent_id, token_value)` → `AgentApiToken | None` — hash lookup, active check, bumps `last_used_at`.
- `connect_agent_api(session, producer_agent_id, user_id, data, is_superuser)` → `ConnectAgentApiResponse` — mints token + creates the `agent_api` credential stamped with `user_workspace_id` (see workspace derivation rule below) + back-fills `token.credential_id` so the token is bound to the credential (cascade) + optionally links the credential to a consumer agent. Raises `AgentApiTokenError(400)` if `agent_api_enabled=false`.

  **Workspace derivation rule (consumer-first):** the `user_workspace_id` on the created credential is derived as follows: (1) if `data.consumer_agent_id` is provided, use the consumer agent's `user_workspace_id`; (2) otherwise use the producer agent's `user_workspace_id`; (3) if neither agent has a workspace, the credential's `user_workspace_id` stays `NULL` (default workspace, unchanged from pre-feature behaviour). The credential is created with `allow_sharing=False` — the owner enables sharing explicitly afterwards.

  **Consumer agent ownership validated up front:** when `data.consumer_agent_id` is provided, the service immediately looks it up (`session.get(Agent, consumer_agent_id)`). If the agent does not exist it raises `AgentApiTokenError(404, "Consumer agent not found")`; if it exists but the caller does not own it (and is not superuser) it raises `AgentApiTokenError(403, "You do not own the consumer agent")`. This check runs **before** any token or credential is minted, so a rejected request leaves no orphaned credential behind.
- `get_connection_info(session, credential_id, user_id, is_superuser)` → `AgentApiConnectionInfo` — decrypts the credential, resolves the producer agent name, reads `read_only` from the bound token, and lists linked consumer agents (with `ui_color_preset`). Owner-only (404 otherwise). Drives the credential detail page.
- `list_producer_connections(session, agent_id, user_id, is_superuser)` → `list[AgentApiProducerConnection]` — one entry per token on this producer, each with its credential name + linked consumer agents (with `ui_color_preset`). Drives the producer card's Connections list.
- `delete_producer_connection(session, agent_id, token_id, user_id, is_superuser)` — disconnect: deletes the bound credential via `CredentialsService.delete_credential` (cascade-deletes the token + triggers the credential-removed sync), or deletes an orphaned token directly. Owner-only.
- `_verify_agent_ownership(session, agent_id, user_id, is_superuser)` — returns agent or raises `AgentApiTokenNotFoundError` (404, no existence leak).

### Docker Adapter (`backend/app/services/environments/adapters/docker_adapter.py`)

- `get_agent_api_status()` → `dict` — `GET {base_url}/agent-api/_status`
- `get_agent_api_spec()` → `dict` — `GET {base_url}/agent-api/openapi.json` (import-only harvest in env-core; no serving child)
- `proxy_agent_api(method, path, headers, body, stream)` → `(status_code, resp_headers, stream)` — `ANY {base_url}/agent-api/proxy/{path}` with full header/body/streaming passthrough; supports multipart

---

## Env-Core: `cinna_api` SDK

Located at `backend/app/env-templates/app_core_base/core/cinna_api/`. Importable inside the container as `cinna_api` (placed on `PYTHONPATH`).

### Public Surface (`__init__.py`)

```python
from cinna_api import api, credentials, error
from cinna_api import UploadFile, File, Query, Body, StreamingResponse, BaseModel, Field
```

- `api` — a pre-created `APIRouter`. `@api.get/post/put/patch/delete(...)` are pass-throughs to FastAPI decorators; all FastAPI parameter parsing, validation, and schema generation applies unchanged.
- `credentials` — typed accessor over `/app/workspace/credentials/credentials.json`. **Reads the file fresh on every call** — the serving child is long-running; caching at import would serve stale secrets across an OAuth refresh or credential resync.
- `error(status, detail)` — structured JSON error helper.
- Ergonomic re-exports: `UploadFile`, `File`, `Query`, `Body`, `StreamingResponse`, `BaseModel`, `Field`.

### Workspace Layout

```
/app/workspace/agent_api/
├── app.py            # optional explicit entrypoint; takes precedence if defined
├── orders.py         # decorated endpoint functions
├── policy.yaml       # platform-enforced guardrails
├── requirements.txt  # optional extra deps → installed into agent_api/.venv
└── README.md         # optional documentation
```

Discovery (`discovery.py`, `build_app(base_url)`): env-core imports every `*.py` module under `agent_api/` whose name does not start with `__` (single-underscore files are **not** skipped), triggering `@api.*` registration on the shared singleton `cinna_api.api` router. It then builds a fresh `FastAPI()` and mounts `api` as a router. If `app.py` defines its own `app = FastAPI()`, that takes precedence (the author controls its `title`/etc directly).

- **Title/description/version** come from `_collect_api_metadata()` — module-level `API_TITLE` / `API_DESCRIPTION` / `API_VERSION` constants found in any imported module (first non-empty wins per field), falling back to generic defaults (`"Agent API"`, etc.). `build_app` takes **no** `agent_name`/`ENV_NAME` (an earlier version used the env image name, which leaked an internal name into the spec).
- **Idempotency + de-dup:** `build_app` calls `cinna_api.api.routes.clear()` before re-importing (so a second build in one process doesn't accumulate), and `_dedupe_routes()` after importing drops duplicate `(path, methods)` routes. This makes the harvest tolerant of a stray non-endpoint file that re-imports an endpoint module (which would otherwise register every route twice → operationId collision → `app.openapi()` failure). Dropped duplicates are logged as a warning.
- `serve.py` and `harvest.py` call `build_app(base_url=...)` with the consumer-facing `CINNA_API_BASE_URL` only.

### Supervised Process Lifecycle (`supervisor.py`)

`AgentApiSupervisor` class (`agent_api_supervisor` singleton):

- **Lazy spawn-on-first-call:** child is NOT started on env start. On first proxied request to `/agent-api/proxy/{path}`, `ensure_running()` spawns `uvicorn core.cinna_api.serve:app --host 127.0.0.1 --port 9100 --reload --reload-dir agent_api` (internal port only, never exposed outside the container). If the child is not healthy within `STARTUP_TIMEOUT=20s`, returns `503 + Retry-After`.
- **Idle reap:** `_reap_loop` stops the child after `IDLE_REAP_SECONDS=300s` (5 min) without API traffic. Reaper check interval: `REAP_CHECK_INTERVAL=30s`.
- **Health check:** `_is_healthy()` pings `GET {BASE_URL}/openapi.json` (FastAPI's built-in; always exists). Polls every 0.5s up to `STARTUP_TIMEOUT`.
- **Boot-error capture:** `_drain_stderr()` keeps a bounded 100-line tail of stderr. Lines containing "Error", "Traceback", or "Exception" update `_boot_error`. This is surfaced via `get_status()` and `harvest_spec()`.
- **Import-only spec harvest:** `harvest_spec()` runs `python -m core.cinna_api.harvest` in a short-lived subprocess (timeout `HARVEST_TIMEOUT=30s`). This imports the agent modules and calls `app.openapi()` WITHOUT spawning the serving child, then writes a single JSON object to stdout (`{"ok": true, "spec": …}` or `{"ok": false, "error": …, "traceback": …}`); the supervisor `json.loads` it. `harvest.py` wraps the build+`openapi()` in `contextlib.redirect_stdout(sys.stderr)` so any agent `print`, library logging, or warning (e.g. FastAPI's "Duplicate Operation ID") goes to stderr and can **never** corrupt the stdout JSON channel — the prior cause of the cryptic "spec harvest produced no JSON. stderr: …" failure. The final JSON is written to the saved real stdout. Errors are captured in `_boot_error`.
- **Reload notification:** after a child restart or harvest, `notify_backend_reload()` posts to `{BACKEND_URL}/api/v1/environments/{ENV_ID}/agent-api-reloaded` so the backend re-harvests and re-caches the spec (and emits `AGENT_API_STATUS_CHANGED`).
- **Optional isolated venv:** if `agent_api/requirements.txt` exists, `_ensure_venv()` creates `agent_api/.venv` via `uv venv --system-site-packages` and installs deps with `uv pip install -r requirements.txt`. Install timeout: `VENV_INSTALL_TIMEOUT=180s`. A requirements hash marker (`agent_api/.venv/.cinna_req_sha256`) skips reinstall when the file is unchanged. Zero-install (no requirements.txt) is the MVP fast path.
  - **Interpreter for harvest + child:** when there is no requirements venv, both the harvest subprocess and the uvicorn child run under **`sys.executable`** (env-core's own interpreter), NOT a bare `python`/`uvicorn` from `PATH`. On the agent base image env-core runs from `/app/.venv` (via `fastapi run`), so a bare `uvicorn` is not on `PATH` and the default `/usr/local/bin/python` lacks fastapi — a bare command crashes the child, env-core returns `502`, and the consumer sees `503`. Using `sys.executable` guarantees the child/harvest share env-core's deps.

### Env-Core Routes (`server/routes.py`)

| Method | Path | Handler | Notes |
|--------|------|---------|-------|
| `GET` | `/agent-api/_status` | `get_agent_api_status` | Returns `get_status()` dict; no child spawn |
| `GET` | `/agent-api/openapi.json` | `get_agent_api_spec` | Import-only harvest via supervisor; caches result |
| `ANY` | `/agent-api/proxy/{path:path}` | `proxy_agent_api` | `ensure_running()` lazily spawns child; 503 while booting |
| `POST` | `/environments/{env_id}/agent-api-reloaded` | `agent_api_reloaded` | Called by supervisor after reload; triggers `AgentApiService.refresh_spec_cache()` |

---

## `policy.yaml` Reference

```yaml
# All keys are optional. Defaults apply to anything omitted.
read_only: true              # Reject non-GET/HEAD (default: true)
allowed_methods:             # Explicit allowlist overrides read_only
  - GET
  - POST
auth: required               # "required" = token mandatory (default: required)
max_body_bytes: 10485760     # Body cap in bytes (default: 10MB)
rate_limit: "60/min"         # Per-token sliding window (default: 60/min)
expose_spec: true            # Allow /openapi.json passthrough (default: true)
allowed_paths:               # Optional path-prefix allowlist (default: ["*"])
  - "/orders"
  - "/products"
```

Unknown keys are silently ignored. An empty or missing file applies `DEFAULT_POLICY`. A parse error applies `FAIL_CLOSED_POLICY` (deny-all) — this is the fail-closed principle from the credentials whitelist.

---

## Frontend Components

- `AgentRestApiCard.tsx` — producer "Agent REST API" card: enable toggle, status badge from `["agentApiStatus", agentId]` (live-updated via `AGENT_API_STATUS_CHANGED`), a View Spec + Refresh button row (View Spec → `openAgentApiSpec(agentId)` opens a new tab; Refresh → `refreshAgentApiStatus` re-harvests spec + re-parses policy, seeds `["agentApiStatus", agentId]`, invalidates `["agentApiSpec", agentId]`, and toasts), and the **Connections** list from `["agentApiConnections", agentId]`. Each row renders consumer agents as Bot badges (`getColorPreset(ui_color_preset)`) with a Disconnect (`AlertDialog` → `deleteAgentApiConnection`) button. No token management UI. On `state === "error"` it shows the compact `summarizeBootError(last_error)` line with a **Details** toggle and a **Retry** button; Retry shares the same `refreshMutation` so a sticky error clears immediately.
- `AgentApiConnectionView.tsx` — `agent_api` credential detail panel: fetches `["agentApiConnection", credentialId]` (`readAgentApiConnection`), shows producer + consumer Bot badges + View Spec (`openAgentApiSpec(producerAgentId)` → new tab) + editable name/notes.
- `OpenApiSpecViewer.tsx` / `routes/agent-api-spec/$agentId.tsx` — rendered, read-only spec viewer opened by View Spec; the route reuses `useAgentApiSpec(agentId)` (`["agentApiSpec", agentId]`). See [spec_viewer_tech.md](spec_viewer_tech.md).
- `ConnectAgentApiDialog.tsx` — wraps `AgentSelectorDialog`; selecting an API-enabled producer (excluding the current agent) calls `connectAgentApi` then invalidates `["credentials"]` + `["agentApiConnections", producerId]`.
- `AgentEnvironmentsTab.tsx` — `agent_api_enabled` switch alongside `webapp_enabled`.
- `EnvironmentPanel.tsx` — "Agent API" tab in the workspace file tree for browsing `agent_api/` files.

### React Query Keys

- `["agentApiStatus", agentId]` — live build/run status (producer card)
- `["agentApiConnections", agentId]` — producer's connection list
- `["agentApiConnection", credentialId]` — single connection detail (credential page)
- `["agentApiSpec", agentId]` — harvested spec

---

## `AGENT_API_STATUS_CHANGED` Event

Emitted by `AgentApiService._fire_status_changed_event()` after a spec reload or boot error change. Sent to the producer agent's owner via the existing WebSocket event bus.

Event `meta` payload:
```json
{
  "agent_id": "<uuid>",
  "environment_id": "<uuid>",
  "state": "running | error",
  "last_error": "null or error message"
}
```

---

## Test Coverage

`backend/tests/api/agents/agents_agent_api_test.py` — 31 scenario-based API tests. Tokens are minted via the connect helper (the raw token is read back from the created credential's data, exactly as a consumer obtains it); "revoke" = delete the credential. Covered:

- Toggle gates routes (404 when disabled); `_status` reports `disabled` regardless
- Connect mints a token + creates an `agent_api` credential (prefix + base_url + spec_url); raw token readable only from the credential's decrypted data
- Connection lifecycle: connect → token authenticates a consumer call → delete credential → token cascade-deleted → 401
- Connection-info endpoint reports producer + consumers + `read_only`; non-owner → 404
- Producer connections list: empty → linked (with consumer `ui_color_preset`) → unlinked; disconnect via `DELETE /connections/{token_id}` revokes the token (401) and drops the row; non-owner → 404
- Hash lookup validates; disconnected/wrong-agent/garbage token → 401
- Policy: `read_only` blocks non-GET/HEAD (405); body cap (413); rate limit (429); `expose_spec=false` (403)
- Policy: `read_only_override` on token only narrows, never widens
- Invalid/malformed `policy.yaml` fails closed (deny-all → 405 even for GET)
- Spec passthrough reflects stubbed live app spec
- Owner proxy passthrough GET + auto-activates suspended env
- Consumer proxy GET passthrough succeeds
- Cold start: consumer call to a **suspended** producer env auto-activates, waits up to the grace window, then forwards (200)
- Cold start: consumer call to a **stopped** producer env auto-activates too (parity with suspended)
- Cold start: activation failure (env → `error`) → consumer gets 503
- Cold start: env still not running after the grace window → consumer gets 503
- `agent_api` credential whitelist exposes correct fields; `token` is redacted in README
- `agent_api` credential syncs into consumer container
- Connect helper blocked when `agent_api_enabled=false` (400); requires producer ownership (404)
- Request-loop: hop-depth header blocks calls beyond `MAX_HOP_DEPTH=4`
- Deadline header propagated and decremented per hop
- Multiple connections: independent disconnect
- Owner routes (connect / connections / status / spec) reject unauthenticated

**In-container coverage gap:** the riskiest code — `AgentApiSupervisor`, `cinna_api` discovery/spec-harvest, `policy.yaml` parsing inside env-core — runs inside the container and is not reachable by the API-only backend suite (the backend tests use `EnvironmentTestAdapter` stubs). This code is covered by manual/env-core verification. The API-only list above covers only the backend proxy edge.

---

*Last updated: 2026-06-04*
