# Agent REST API — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/agent_api/agent_api_token.py` — `AgentApiToken` (table, with `credential_id` FK), `AgentApiTokenBase`, `AgentApiTokenCreate`, `AgentApiTokenPublic`, `AgentApiTokenCreated`, `AgentApiConnectedAgent`, `AgentApiConnectionInfo`, `AgentApiProducerConnection`, `AgentApiProducerConnections`, `ConnectAgentApiRequest`, `ConnectAgentApiResponse` (no manual token-CRUD models — `AgentApiTokenUpdate`/`AgentApiTokensPublic` were removed)
- `backend/app/models/agents/agent.py` — `agent_api_enabled: bool` and `agent_api_identity_enabled: bool` fields on `Agent` (table), `AgentUpdate`, and `AgentPublic`
- `backend/app/models/agent_api/agent_api_access_grant.py` (new) — `AgentApiAccessGrant` (table), `AgentApiAccessGrantBase/Create/Update/Public`, `AgentApiAccessGrantsPublic`, `AgentApiGrantUser`, plus the scope-catalog models `AgentApiScope` / `AgentApiScopeCatalog`
- `backend/app/models/environments/environment.py` — `agent_api_spec_parsed`, `agent_api_spec_fetched_at`, `agent_api_spec_error`, `agent_api_policy_cache` cache columns on `AgentEnvironment`
- `backend/app/models/credentials/credential.py` — `AGENT_API` added to `CredentialType` enum
- `backend/app/models/__init__.py` — re-exports all agent_api models

### Backend — Routes

- `backend/app/api/routes/agent_api.py` — owner-preview routes + connect helper + producer connections list/delete + the **owner-gated grant routes** (`/grants*`); prefix `/api/v1/agents/{agent_id}/agent-api`
- `backend/app/api/routes/agent_api_public.py` — consumer serving routes (token auth); prefix `/api/v1/agent-api/{agent_id}`. The `consumer_proxy` handler also does the L2 identity verify / strip / inject (see [Caller Identity](#caller-identity--producer-scopes))
- `backend/app/api/routes/credentials.py` — `GET /credentials/{id}/agent-api-connection` (connection detail for the credential page)
- `backend/app/api/main.py` — both agent_api routers registered

### Backend — Services

- `backend/app/services/agent_api/agent_api_service.py` — `AgentApiService` + exception hierarchy; `policy.yaml` `scopes:` parsing + optional edge scope enforcement
- `backend/app/services/agent_api/agent_api_token_service.py` — `AgentApiTokenService`
- `backend/app/services/agent_api/agent_api_identity_service.py` (new) — `AgentApiIdentityService`: mints/verifies the L2 `owner_identity_token` JWT, builds the synthetic credentials.json entry, resolves the trusted `X-Cinna-Caller-*` headers; exports the header-name constants shared with the proxy
- `backend/app/services/agent_api/agent_api_grant_service.py` (new) — `AgentApiGrantService`: owner-gated grant CRUD, scope sanitization, scope catalog read, live per-call scope resolution, SecurityEvent audit
- `backend/app/services/environments/adapters/docker_adapter.py` — `get_agent_api_status()`, `get_agent_api_spec()`, `proxy_agent_api()`
- `backend/app/services/environments/environment_lifecycle.py` — `agent_api/` added to `dirs_to_copy`
- `backend/app/services/credentials/credentials_service.py` — `AGENT_ENV_ALLOWED_FIELDS["agent_api"]` + `SENSITIVE_FIELDS["agent_api"]` + `_rewrite_agent_api_urls_for_env()` (swaps the stored public URL host for `AGENT_ENV_BACKEND_URL` on env sync); injects the synthetic `owner_identity_token` block in `prepare_credentials_for_environment()` and documents it in `generate_credentials_readme`
- `backend/app/core/config.py` — `AGENT_ENV_BACKEND_URL` (container-reachable backend origin; also the env's `BACKEND_URL`); `AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS` (default `30` — TTL of the L2 identity token)
- `backend/app/models/events/security_event.py` — `AGENT_API_GRANT_CREATED` / `AGENT_API_GRANT_UPDATED` / `AGENT_API_GRANT_DELETED` event-type constants

### Backend — Migrations

| Revision ID | File | What it does |
|-------------|------|-------------|
| `aa11agentapi01` | `aa11agentapi01_add_agent_api_enabled_to_agent.py` | Adds `agent_api_enabled BOOL NOT NULL DEFAULT false` to `agent` |
| `aa22agentapi02` | `aa22agentapi02_add_agent_api_cache_to_environment.py` | Adds `agent_api_spec_parsed` (JSON), `agent_api_spec_fetched_at`, `agent_api_spec_error` (VARCHAR 512), `agent_api_policy_cache` (JSON) to `agent_environment` |
| `aa33agentapi03` | `aa33agentapi03_add_agent_api_token_table.py` | Creates `agent_api_token` table, including the `credential_id` FK (`ON DELETE CASCADE`) + its index (see schema below) |
| `aa44agentapi04` | `aa44agentapi04_add_agent_api_to_credential_type_enum.py` | `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'AGENT_API'` (non-transactional, mirrors prior enum migrations) |

| `c7e2a9f4b1d8` | `c7e2a9f4b1d8_backfill_agent_api_credential_workspace.py` | **Data-only backfill** for Automatic Credentials grouping. Sets `user_workspace_id` on legacy `agent_api` credentials where it is currently `NULL` and the credential is linked to exactly one agent that carries a non-null workspace. Leaves `NULL` when the link is ambiguous (zero links, multiple links, or linked agent is in the default workspace). Downgrade is a no-op. Credentials that were already `NULL` and stay `NULL` appear in the Automatic Credentials section only under the default-workspace / unfiltered view — correct grouping, no data loss. |

| `25a74abc7f4a` | `25a74abc7f4a_add_agent_api_access_grant_table_and_.py` | Creates the `agent_api_access_grant` table (see schema below) with its unique constraint and the `producer_agent_id` / `user_id` indexes, and adds `agent_api_identity_enabled BOOL NOT NULL DEFAULT false` to `agent` (added with a `server_default` to backfill existing rows, then the default is dropped so the model-level `Field(default=False)` is the source of truth). `down_revision = c70a14722869`. Hand-trims an unrelated autogen drift on `cli_device_login_request` (a pre-existing `TIMESTAMP → DateTime` diff not introduced by this change). |

Chain: `aa11 → aa22 → aa33 → aa44`. These migrations were authored together and never released; `aa33` was edited in place to carry `credential_id` (no separate migration). `c7e2a9f4b1d8` is a later, independent data-only migration. `25a74abc7f4a` is the caller-identity / scopes migration (descends from `c70a14722869`). A fresh `alembic upgrade head` yields the current schema.

### Env-Core (inside container)

- `backend/app/env-templates/app_core_base/core/cinna_api/__init__.py` — SDK public surface: `api`, `credentials`, `error`, ergonomic re-exports
- `backend/app/env-templates/app_core_base/core/cinna_api/credentials.py` — fresh-read `credentials.json` accessor
- `backend/app/env-templates/app_core_base/core/cinna_api/caller.py` (new) — request-scoped `caller` dependency + `Caller` dataclass; reads the trusted `X-Cinna-Caller-*` headers (**needs env rebuild**)
- `backend/app/env-templates/app_core_base/core/cinna_api/errors.py` — `error()` structured-error helper
- `backend/app/env-templates/app_core_base/core/cinna_api/supervisor.py` — `AgentApiSupervisor` class + `agent_api_supervisor` singleton
- `backend/app/env-templates/app_core_base/core/cinna_api/discovery.py` — module discovery and FastAPI app construction
- `backend/app/env-templates/app_core_base/core/cinna_api/harvest.py` — import-only spec harvest subprocess entry point (`python -m core.cinna_api.harvest`)
- `backend/app/env-templates/app_core_base/core/cinna_api/serve.py` — `app = FastAPI()` entry point for the uvicorn child
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `GET /agent-api/_status`, `GET /agent-api/openapi.json`, `ANY /agent-api/proxy/{path}`, `POST /environments/{id}/agent-api-reloaded`
- `backend/app/env-templates/app_core_base/core/scripts/scaffold_agent_api.py` — scaffolder that writes a working `orders.py` (incl. `API_TITLE`/`API_DESCRIPTION`/`API_VERSION` example constants) + `policy.yaml` stub
- `backend/app/env-templates/app_core_base/core/prompts/REST_API_BUILDING.md` — the building guide injected into the producer agent (SDK usage, spec-naming constants, policy, stray-file pitfall, scaffolding)

### Frontend

- `frontend/src/components/Agents/AgentRestApiCard.tsx` — producer "Agent REST API" card: enable toggle, error banner (compact summary + Details toggle + Retry), a View Spec + Refresh button row (View Spec opens the spec viewer tab via `openAgentApiSpec()`; Refresh calls `refreshAgentApiStatus` to re-harvest the spec + re-parse policy on demand), and the Connections list (consumer Bot badges + owner email + Disconnect). Hosted by `AgentIntegrationsTab.tsx`. Module helpers `parseHttpStatus()` / `summarizeBootError()` turn a raw `last_error` into the one-line summary.
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — renders `AgentRestApiCard`; passes `agentApiIdentityEnabled` into `AgentApiAccessScopesCard`
- `frontend/src/components/Agents/AgentApiAccessScopesCard.tsx` (new) — the "Access & Scopes" card: opt-in switch (`toggleIdentity` → `agent_api_identity_enabled`), `UserAllowlistPicker` (passes `includeSelf` — owner-grants-self is valid here, unlike share/assignment pickers) for adding granted users, and per-user scope chips (`policy.yaml` catalog quick-add + free-text). Drives `["agentApiGrants", agentId]` and `["agentApiScopeCatalog", agentId]` queries and the grant create/update/delete mutations.
- `frontend/src/components/Credentials/AgentApiConnectionView.tsx` — `agent_api` credential detail panel, a single card split into two columns: **left** = editable name/notes form (metadata-only `updateCredential`); **right** = a "Producer" header row (producer rendered via the shared `AgentBadge` — the whole pill is clickable/colour-tinted, not just a label — plus an optional `read-only` badge and a **View Spec** button pushed to the right next to the producer, no raw `base_url` shown) above a compact bordered list of connected consumer agents, each row = `AgentBadge` + the owner's `name · email` (mirrors the A2A access-token list style; no table header). Owner name+email disambiguate identical agent names across bundle installs.
- `frontend/src/routes/agent-api-spec/$agentId.tsx`, `frontend/src/components/Agents/OpenApiSpecViewer.tsx`, `frontend/src/utils/agentApiSpec.ts` — the rendered spec viewer (route + renderer + launch helper). See [spec_viewer_tech.md](spec_viewer_tech.md).
- `frontend/src/components/Credentials/ConnectAgentApiDialog.tsx` — "Connect Agent API"; thin wrapper over the shared `AgentSelectorDialog`, filtered to API-enabled agents
- `frontend/src/components/Common/AgentSelectorDialog.tsx` — shared agent picker (Bot badge + colour preset) reused by the connect dialog
- `frontend/src/components/Credentials/AddCredential.tsx` — "Connect Agent API" entry point in the add-credential picker
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` — per-agent "Connect Agent API" button (passes the consumer agent id)
- `frontend/src/components/Credentials/credentialTypes.ts` — `agent_api` display-only type meta (icon/label/badge); not offered in the manual picker
- `frontend/src/routes/_layout/credential/$credentialId.tsx` — `agent_api` branch renders `AgentApiConnectionView` (the two-column card owns the editable name/notes form — `updateCredential` with `{name, notes}` only, never the proxy token) followed by `CredentialSharing`. `CredentialTemplateSharing` is **not rendered** for `agent_api` credentials — template sharing is meaningless for a connection credential that has no user-fillable private fields.
- `frontend/src/routes/_layout/credentials.tsx` — partitions the single `["credentials", workspaceFilter]` query result into two sections: **My Credentials** (`type !== "agent_api"`) and **Automatic Credentials** (`type === "agent_api"`). The Automatic Credentials section is hidden when empty; when visible it shows the same `CredentialGrid` component with a one-line explainer ("Connections created by 'Connect Agent API'. Manage name, notes, and sharing here."). Workspace filter applies automatically because the query is shared and agent_api credentials are now workspace-stamped at connect time.
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` — `agent_api_enabled` toggle
- `frontend/src/components/Environment/EnvironmentPanel.tsx` — "Agent API" tab in the workspace file tree

### Tests

- `backend/tests/api/agents/agents_agent_api_test.py` — 31 scenario-based API tests (see test coverage section below)
- `backend/tests/api/agents/agents_agent_api_grants_test.py` — 15 caller-identity + scopes tests (see test coverage section below)

---

## Database Schema

### `agent` table (modified)

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `agent_api_enabled` | `BOOL NOT NULL` | `false` | Whether the Agent REST API feature is active for this agent |
| `agent_api_identity_enabled` | `BOOL NOT NULL` | `false` | Producer opt-in for **per-user scopes** + optional edge enforcement. Identity *attribution* headers are injected regardless; only `X-Cinna-Caller-Scopes` and edge scope enforcement are gated by this flag |

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

### `agent_api_access_grant` table (new)

One row per `(producer_agent_id, user_id)` granting a platform user a set of scope names on a producer's API. Carries **no secret** — only scope names.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID PK` | |
| `producer_agent_id` | `UUID FK → agent` | The API-exposing agent (Agent A); `ON DELETE CASCADE`; indexed |
| `user_id` | `UUID FK → user` | The granted cinna-core user; `ON DELETE CASCADE`; indexed |
| `scopes` | `JSON` (list[str]) | Scope names from the producer's `policy.yaml` catalog. Empty list = "known user, no scopes" |
| `created_by` | `UUID FK → user` | The producer owner who created the grant (audit/provenance); `ON DELETE CASCADE` |
| `created_at` | `DATETIME NOT NULL` | |
| `updated_at` | `DATETIME NOT NULL` | |

Constraints: unique `(producer_agent_id, user_id)` (`uq_agent_api_access_grant_producer_user`) — one grant per (producer, user); btree indexes on `producer_agent_id` and `user_id`.

### `credential` table (enum modified)

`CredentialType` PostgreSQL native enum extended with `'AGENT_API'`. The `credential_data` (encrypted) shape for this type: `{ base_url, token, spec_url, label, producer_agent_id }`.

> The L2 `owner_identity_token` is **not** a `CredentialType` and has **no DB row** — it is a synthetic, host-computed credentials.json entry (`id="owner_identity"`, `type="owner_identity_token"`), mirroring `current_user`. See [Caller Identity](#caller-identity--producer-scopes).

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
| `POST` | `/_refresh` | Force an import-only re-harvest (`get_spec(force_refresh=True)` + `load_policy(force_refresh=True)`) and return the updated status. Refreshes the cached spec **and** re-parses `policy.yaml`, and clears a sticky boot/harvest error that would otherwise only clear on the next automatic re-harvest. **Wake-on-refresh:** when the producer env is not running, the handler first attempts to wake it via `resolve_running_producer_env` (same cold-start machinery used by the consumer proxy: kicks `EnvironmentService.activate_environment`, then blocks up to `ACTIVATION_WAIT_SECONDS` via `_wait_for_running_env`). The re-harvest runs only once the env reaches `running`. On a successful wake, `update_last_activity` is bumped so the newly-woken env is not immediately re-suspended. The whole wake attempt is wrapped in `try/except AgentApiError` — best-effort, never raises; the returned `state` reflects reality (see **State semantics** below). No route signature or response-model change; no client regeneration was needed. Drives the producer card's **Refresh** and **Retry** buttons. |
| `GET` | `/openapi.json` | Harvested spec from cache, or import-only harvest. Requires `agent_api_enabled`. |
| `ANY` | `/proxy/{path:path}` | Full HTTP passthrough for owner testing. Requires running env. No policy enforcement (owner-only). Excluded from OpenAPI schema. |
| `POST` | `/connect` | "Connect Agent API" helper — mints token + creates connection credential + optional consumer link. Returns `ConnectAgentApiResponse`. |
| `GET` | `/connections` | List the connections (consumers) of this producer. Returns `AgentApiProducerConnections`. |
| `DELETE` | `/connections/{token_id}` | Disconnect — deletes the connection credential (cascade-deletes the token) or an orphaned token directly. |
| `GET` | `/grants/scope-catalog` | Available scopes the producer declared in `policy.yaml`, projected to `{name, description}` for the picker. Graceful empty catalog. Returns `AgentApiScopeCatalog`. |
| `GET` | `/grants` | List per-user access grants for this producer. Each grant resolves the granted user's `{id, email, full_name}`. Returns `AgentApiAccessGrantsPublic` (`count` = page size = list length). |
| `POST` | `/grants` | Create a grant for `(producer, body.user_id)`. 404 if the granted user does not exist; 409 if a grant for that user already exists. Returns `AgentApiAccessGrantPublic`. |
| `PUT` | `/grants/{grant_id}` | Update a grant's `scopes` (identity is immutable). Takes effect on the next call. Returns `AgentApiAccessGrantPublic`. |
| `DELETE` | `/grants/{grant_id}` | Remove a grant. Takes effect on the next call. Returns `Message`. |

All `/grants*` routes are **owner-gated** via `AgentApiService.resolve_agent_only` (404 — no existence leak — for a non-owner or missing agent; superuser bypasses ownership). There are **no** token-CRUD routes; tokens are created only via `/connect` and removed only via `/connections/{token_id}` (or by deleting the credential).

### Credential Connection Detail (`backend/app/api/routes/credentials.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/credentials/{id}/agent-api-connection` | Connection detail for an `agent_api` credential: producer agent (`producer_agent_name` + `producer_ui_color_preset` for the `AgentBadge`), `base_url`/`spec_url`, `read_only`, and linked consumer agents (with `ui_color_preset`, `owner_name`, and `owner_email`). Returns `AgentApiConnectionInfo`. Owner-only (404 on non-owner). |

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
- `get_status(session, agent, environment)` → `dict` — reports `state`, spec availability, last error, policy summary, and `spec_fetched_at` (ISO timestamp of the last successful harvest, from `agent_api_spec_fetched_at`). `state` tracks the *serving child's* current health (see **State semantics** below) while `spec_fetched_at` dates the *cached spec* — the two are reported separately so a stale spec is visible instead of masquerading as current. Never spawns the serving child.
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
- `get_connection_info(session, credential_id, user_id, is_superuser)` → `AgentApiConnectionInfo` — decrypts the credential, resolves the producer agent name + `producer_ui_color_preset` (so the frontend renders the producer with the same `AgentBadge` as any other agent), reads `read_only` from the bound token, and lists linked consumer agents (via `_build_connected_agent`, with `ui_color_preset`, `owner_name`, and `owner_email`). Owner-only (404 otherwise). Drives the credential detail page.
- `list_producer_connections(session, agent_id, user_id, is_superuser)` → `list[AgentApiProducerConnection]` — one entry per token on this producer, each with its credential name + linked consumer agents (via `_build_connected_agent`, with `ui_color_preset`, `owner_name`, and `owner_email`). Drives the producer card's Connections list.
- `_build_connected_agent(session, agent)` → `AgentApiConnectedAgent` — shared projection helper that resolves the agent owner's name + email (`session.get(User, agent.owner_id)` → `full_name`/`email`) so identical agent names stay distinguishable in the UI. Used by both retrieval methods above.
- `delete_producer_connection(session, agent_id, token_id, user_id, is_superuser)` — disconnect: deletes the bound credential via `CredentialsService.delete_credential` (cascade-deletes the token + triggers the credential-removed sync), or deletes an orphaned token directly. Owner-only.
- `_verify_agent_ownership(session, agent_id, user_id, is_superuser)` — returns agent or raises `AgentApiTokenNotFoundError` (404, no existence leak).

### Docker Adapter (`backend/app/services/environments/adapters/docker_adapter.py`)

- `get_agent_api_status()` → `dict` — `GET {base_url}/agent-api/_status`
- `get_agent_api_spec()` → `dict` — `GET {base_url}/agent-api/openapi.json` (import-only harvest in env-core; no serving child)
- `proxy_agent_api(method, path, headers, body, stream, timeout, query_string)` → `(status_code, resp_headers, stream)` — `ANY {base_url}/agent-api/proxy/{path}` with full header/body/streaming passthrough; supports multipart. `query_string` (raw, already-encoded, no leading `?`) is appended to the env-core URL so the consumer's query params reach the producer's handler; the owner/consumer routes thread `request.url.query` here. Without it the query is silently dropped and `Query(...)` params fall back to defaults.

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
- **Boot-error capture + self-heal:** `_drain_stderr()` keeps a bounded 100-line tail of stderr. Lines containing "Error", "Traceback", or "Exception" update `_boot_error`; an `"Application startup complete"` line **clears** it (a uvicorn `--reload` restart re-imports in-process without `_spawn`, so a fixed file self-heals). `get_status()` additionally treats live child health as ground truth — a healthy child clears any stale `_boot_error` and reports `running`, so a transient boot error never sticks once the API is actually serving (the "poisoned API" symptom). The harvest path's error is persisted separately on the env row (`agent_api_spec_error`).
- **Stale-bytecode guard:** before each harvest and child spawn, `_purge_source_pycache()` removes `__pycache__` under the `agent_api/` source tree (the deps venv's caches are left intact), and both subprocesses run with `PYTHONDONTWRITEBYTECODE=1` (`harvest.py` also sets `sys.dont_write_bytecode`). Fast edit→sync→import cycles otherwise hit CPython's 1-second mtime-resolution `.pyc` reuse, so a corrected file could be shadowed by bytecode from the previous broken version — making `refresh` keep harvesting the old error.
- **Import-only spec harvest:** `harvest_spec()` runs `python -m core.cinna_api.harvest` in a short-lived subprocess (timeout `HARVEST_TIMEOUT=30s`). This imports the agent modules and calls `app.openapi()` WITHOUT spawning the serving child, then writes a single JSON object to stdout (`{"ok": true, "spec": …}` or `{"ok": false, "error": …, "traceback": …}`); the supervisor `json.loads` it. `harvest.py` wraps the build+`openapi()` in `contextlib.redirect_stdout(sys.stderr)` so any agent `print`, library logging, or warning (e.g. FastAPI's "Duplicate Operation ID") goes to stderr and can **never** corrupt the stdout JSON channel — the prior cause of the cryptic "spec harvest produced no JSON. stderr: …" failure. The final JSON is written to the saved real stdout. Errors are captured in `_boot_error`.
- **Reload notification:** after a child restart or harvest, `notify_backend_reload()` posts to `{BACKEND_URL}/api/v1/environments/{ENV_ID}/agent-api-reloaded` so the backend re-harvests and re-caches the spec (and emits `AGENT_API_STATUS_CHANGED`).
- **Optional isolated venv:** if `agent_api/requirements.txt` exists, `_ensure_venv()` creates `agent_api/.venv` via `uv venv --system-site-packages` and installs deps with `uv pip install -r requirements.txt`. Install timeout: `VENV_INSTALL_TIMEOUT=180s`. A requirements hash marker (`agent_api/.venv/.cinna_req_sha256`) skips reinstall when the file is unchanged. Zero-install (no requirements.txt) is the MVP fast path.
  - **Interpreter for harvest + child:** when there is no requirements venv, both the harvest subprocess and the uvicorn child run under **`sys.executable`** (env-core's own interpreter), NOT a bare `python`/`uvicorn` from `PATH`. On the agent base image env-core runs from `/app/.venv` (via `fastapi run`), so a bare `uvicorn` is not on `PATH` and the default `/usr/local/bin/python` lacks fastapi — a bare command crashes the child, env-core returns `502`, and the consumer sees `503`. Using `sys.executable` guarantees the child/harvest share env-core's deps.

### Env-Core Routes (`server/routes.py`)

| Method | Path | Handler | Notes |
|--------|------|---------|-------|
| `GET` | `/agent-api/_status` | `get_agent_api_status` | Returns `get_status()` dict; no child spawn |
| `GET` | `/agent-api/openapi.json` | `get_agent_api_spec` | Import-only harvest via supervisor; caches result |
| `ANY` | `/agent-api/proxy/{path:path}` | `proxy_agent_api` | `ensure_running()` lazily spawns child; 503 while booting. Forwards the **raw** query string (`request.url.query`) to the child so repeated keys (`?tag=a&tag=b`) and exact encoding survive |
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

- `AgentRestApiCard.tsx` — producer "Agent REST API" card: enable toggle, status badge from `["agentApiStatus", agentId]` (live-updated via `AGENT_API_STATUS_CHANGED`), a View Spec + Refresh button row (View Spec → `openAgentApiSpec(agentId)` opens a new tab; Refresh → polls `POST /_refresh` in a bounded loop applying the **terminal-state contract** below — shows **"Waking up agent…"** while `state === "not_running"`, then resolves terminally based on `envRunning` + `spec_available`/`last_error`; seeds `["agentApiStatus", agentId]` and invalidates `["agentApiSpec", agentId]`), and the **Connections** list from `["agentApiConnections", agentId]`. Each row renders consumer agents as Bot badges (`getColorPreset(ui_color_preset)`), each paired with the owner's email (muted text) to disambiguate same-named agents, plus a Disconnect (`AlertDialog` → `deleteAgentApiConnection`) button. No token management UI. On `state === "error"` or `last_error` it shows the compact `summarizeBootError(last_error)` line with a **Details** toggle and a **Retry** button; Retry shares the same refresh loop so a sticky error clears immediately.
- `AgentApiConnectionView.tsx` — `agent_api` credential detail panel: fetches `["agentApiConnection", credentialId]` (`readAgentApiConnection`); two-column card (left = editable name/notes, right = producer `AgentBadge` + View Spec `openAgentApiSpec(producerAgentId)` next to the producer + compact connected-agents list with owner name·email).
- `OpenApiSpecViewer.tsx` / `routes/agent-api-spec/$agentId.tsx` — rendered, read-only spec viewer opened by View Spec; the route fetches the spec directly (interleaved with the wake-poll loop) rather than via `useAgentApiSpec`. See [spec_viewer_tech.md](spec_viewer_tech.md).
- `ConnectAgentApiDialog.tsx` — wraps `AgentSelectorDialog`; selecting an API-enabled producer (excluding the current agent) calls `connectAgentApi` then invalidates `["credentials"]` + `["agentApiConnections", producerId]`.
- `AgentEnvironmentsTab.tsx` — `agent_api_enabled` switch alongside `webapp_enabled`.
- `EnvironmentPanel.tsx` — "Agent API" tab in the workspace file tree for browsing `agent_api/` files.

### State semantics and terminal-state contract

The `state` field in the `/_status` and `/_refresh` response has **two independent layers**:

| `state` value | What it represents |
|---|---|
| `disabled` | `agent_api_enabled` is `false` on the Agent row |
| `not_running` | The **env** (container) is not yet running (starting, suspended, stopped at the env lifecycle level) |
| `running` | The **serving child app** (`uvicorn` subprocess on port 9100) is healthy |
| `stopped` | Env is running but the serving child is idle (reaped after 5 min without proxy traffic) |
| `empty` | Env is running, child is healthy, but no endpoints are exposed |
| `error` | Boot or harvest error on the serving child or the import-only harvest |

**Critical distinction:** `stopped` means the *serving child app* is idle, NOT that the environment is down. The spec is always served from the import-only harvest cache — the serving child is never needed for spec reads. A producer with `state === "stopped"` has a fully usable spec.

**Terminal-state criterion** used by both `AgentRestApiCard.tsx` and `routes/agent-api-spec/$agentId.tsx`:

```
envRunning = (state !== "not_running")     // env lifecycle: is the container up?
harvestFailed = (state === "error") || !!last_error
specReady = !!spec_available && !last_error
```

Decision order (applied once `envRunning` is true — env container is up):

1. **Failure** — `harvestFailed` → toast/show error; stop polling.
2. **Success** — `specReady` → toast/render spec; stop polling. This fires for `state === "running"`, `"stopped"`, `"empty with prior spec"`, etc. — any running-env state where the spec is available and no error is set.
3. **Empty** — env running, no spec, no error → toast "no endpoints yet"; stop polling.

Only `state === "not_running"` (env container genuinely not up yet) keeps the poll alive. `spec_available` and `last_error` are mutually exclusive on the live path — a successful harvest sets `spec_available` and clears `last_error`; a failed one sets `last_error` only.

The `AgentApiStatus` TypeScript interface in `frontend/src/hooks/useAgentApi.ts` mirrors these values: `state: "disabled" | "not_running" | "running" | "error" | "stopped" | "empty"`.

### React Query Keys

- `["agentApiStatus", agentId]` — live build/run status (producer card)
- `["agentApiConnections", agentId]` — producer's connection list
- `["agentApiConnection", credentialId]` — single connection detail (credential page)
- `["agentApiSpec", agentId]` — harvested spec (invalidated by Refresh; no longer backed by a `useAgentApiSpec` hook — the spec route fetches directly)

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

## Caller Identity & Producer Scopes

The business-logic overview is in [agent_api.md → Caller Identity & Producer Scopes](agent_api.md#caller-identity--producer-scopes). This section covers the implementation.

### L2 identity token — `AgentApiIdentityService`

`backend/app/services/agent_api/agent_api_identity_service.py`. Stateless (no table).

- `mint(owner_user_id) -> str` — `security.create_access_token(subject=str(owner_user_id), expires_delta=timedelta(days=AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS), extra_claims={"aud": "agent_api_caller", "type": "agent_api_identity"})`. HS256 / `SECRET_KEY`. Claims: `{ sub, aud="agent_api_caller", type="agent_api_identity", exp, iat }`.
- `verify(token) -> uuid.UUID | None` — `jwt.decode` with `audience="agent_api_caller"`; rejects unless `type == "agent_api_identity"` and `sub` is a UUID. **Never raises** — any failure (missing/malformed/expired/wrong-audience/bad-signature/non-UUID-sub) returns `None` (= anonymous). The raw token is never logged.
- `build_owner_identity_block(owner_user_id) -> dict` — the synthetic credentials.json entry: `{ id:"owner_identity", name:"Owner Identity Token", type:"owner_identity_token", notes:..., credential_data:{ token:<minted JWT>, header:"X-Cinna-Caller-Identity", usage:... } }`. Self-describing (`header` + `usage`) so the building agent reads the entry and sends what it names.
- `resolve_caller_headers(session, identity_token) -> dict[str, str]` — verifies the token, loads the live `User`, returns the trusted attribution headers. Always sets `X-Cinna-Caller-User-Id`; adds `X-Cinna-Caller-Email` / `X-Cinna-Caller-Username` only when present (some HTTP stacks drop empty-valued headers). Returns `{}` (anonymous) for no/invalid token or a user that no longer exists. Scopes are **not** set here — that is the proxy's live grant lookup. Never raises.

**Header-name constants** (exported for the proxy + the SDK to share): `IDENTITY_HEADER = "X-Cinna-Caller-Identity"` (the L2 token's wire header), `CALLER_USER_ID_HEADER` / `CALLER_EMAIL_HEADER` / `CALLER_USERNAME_HEADER` / `CALLER_SCOPES_HEADER` (the injected attribution headers), and `CALLER_HEADER_PREFIX = "x-cinna-caller-"` (the lowercased strip prefix).

### Synthetic credentials.json entry + injection point

In `prepare_credentials_for_environment()` (`credentials_service.py`), after the `current_user` block: if the unfiltered credential list contains **≥1 `agent_api`** entry, the service resolves the agent's owner and appends `AgentApiIdentityService.build_owner_identity_block(owner.id)` to `filtered_credentials`. Guarded — a missing owner logs a warning and is skipped; any exception is swallowed so credential sync never breaks. Both write paths (initial env creation and `sync_credentials_to_agent_environments`) flow through here, so every `credentials.json` write is covered and re-mints the token (freshness, plan D7). The entry is **never redacted**: `generate_credentials_readme` emits an *Owner Identity (agent_api calls)* section that shows the token in full and explains the header to send (the synthetic `current_user` / `owner_identity_token` types are both exempt from the redaction loop).

### Proxy verify / strip / inject — `consumer_proxy`

In `agent_api_public.py`, after token validation (the auth gate, so no grant lookup runs for an unauthenticated request):

1. `caller_headers = AgentApiIdentityService.resolve_caller_headers(session, request.headers.get(IDENTITY_HEADER))` — attribution headers, or `{}` when anonymous.
2. If a user was attributed **and** `agent.agent_api_identity_enabled`: `caller_scopes = AgentApiGrantService.resolve_scopes_for_caller(session, agent_id, owner_user_id)` (owner id read back from the just-set `X-Cinna-Caller-User-Id` — single source of truth, no second token verify). When non-empty, `caller_headers["X-Cinna-Caller-Scopes"] = " ".join(caller_scopes)` (space-separated, OAuth-style).
3. `authorize_consumer_request(..., caller_scopes=caller_scopes)` — runs policy enforcement (incl. optional edge scope enforcement) **before** env resolution.
4. Forward-header construction applies the four security rules: drop `authorization`, drop the identity header (explicitly **and** via the prefix strip — redundant by design), drop **all** inbound `x-cinna-caller-*` headers, then `fwd_headers.update(caller_headers)` (authoritative) and `fwd_headers.update(hop_headers)`. Keys are lowercased before comparison.

Identity attribution is honored regardless of the flag (backward compatible); only scope injection (and edge enforcement) are gated by `agent_api_identity_enabled`.

### `AgentApiGrantService`

`backend/app/services/agent_api/agent_api_grant_service.py`. All CRUD is owner-gated via `_require_owner` → `AgentApiService.resolve_agent_only` (404, no leak).

- `list_grants` / `create_grant` / `update_grant` / `delete_grant` — owner-gated. `create_grant` 404s for a phantom user and 409s for a duplicate (the DB unique constraint backs it). `create`/`update`/`delete` each write a `SecurityEvent` (`AGENT_API_GRANT_CREATED/_UPDATED/_DELETED`, severity `medium`, details `{grant_id, granted_user_id, scopes}`) — best-effort, never raises.
- `get_scope_catalog` — reads the canonical `policy["scopes"]` (already-normalized at parse time) via `AgentApiService.get_effective_policy`; projects to `{name, description}` (the `requires` patterns are platform-internal and not surfaced). Graceful empty catalog.
- `resolve_scopes_for_caller(session, producer_agent_id, owner_user_id) -> list[str]` — the LIVE per-call lookup the proxy uses. No grant ⇒ `[]`. Never raises (a lookup failure degrades to no scopes).
- `to_public` — projects a grant + resolved user for the API.
- `_sanitize_scopes` — scope names are transported space-separated, so each name is trimmed, names with inner whitespace or empties are dropped, and the list is de-duplicated preserving order. Applied at the create/update write boundary.

### Scope encoding on the wire

`X-Cinna-Caller-Scopes` is a single **space-separated** list (OAuth-style); scope names are opaque tokens with no internal whitespace (enforced by `_sanitize_scopes`). The SDK's `_parse_scopes` splits on whitespace.

### `policy.yaml` `scopes:` catalog + edge enforcement

`AgentApiService._parse_scopes_catalog` (called from `parse_policy`) normalizes three author forms into the canonical `[{name, description, requires:[{method, path}]}]` SSOT consumed by both the catalog reader and edge enforcement:

1. **Bare list** of names (`["orders.read", "orders.write"]`) → documentation-only scopes (`requires:[]`).
2. **`{name: description}` mapping** → documentation-only scopes.
3. **Rich `{name: {description, requires:[{method, path}]}}` mapping** → edge-enforceable scopes. `_parse_scope_requires` normalizes each requirement to `{method, path}` (method optional, upper-cased, defaults to `*`).

Parsing is graceful entry-by-entry — a malformed catalog degrades to empty/partial ("no edge enforcement"), never a closed policy. (A malformed *whole* `policy.yaml` still fails closed via `FAIL_CLOSED_POLICY`.) `DEFAULT_POLICY` / `FAIL_CLOSED_POLICY` carry `"scopes": []`.

**`_enforce_scopes(policy, method, path, caller_scopes, identity_enabled)`** — optional defense-in-depth, called from `enforce_policy`. Conservative opt-in: it returns immediately unless `identity_enabled` is `True`, and only fires for scopes that declare a non-empty `requires:`. For a matching `(method, path)` pattern it raises `AgentApiPolicyError(403)` unless `caller_scopes` carries the scope. Path matching is **segment-accurate** (`/orders` gates `/orders` and `/orders/123` but not `/orders-archive`). The producer remains the **primary** enforcer; identity OFF or a documentation-only catalog is never edge-denied.

`authorize_consumer_request` and `enforce_policy` gained `caller_scopes` and `identity_enabled` parameters threaded from the proxy.

### SDK `caller` accessor

`backend/app/env-templates/app_core_base/core/cinna_api/caller.py` (re-exported from `cinna_api`). `caller = Depends(_resolve_caller)` is a FastAPI dependency building a frozen `Caller` dataclass from the trusted headers:

| Field / method | Source |
|---|---|
| `user_id` | `X-Cinna-Caller-User-Id` (or `None`) |
| `email` | `X-Cinna-Caller-Email` |
| `username` | `X-Cinna-Caller-Username` |
| `scopes: list[str]` | `X-Cinna-Caller-Scopes` split on whitespace |
| `is_anonymous` | `user_id is None` |
| `has_scope(name)` | `name in scopes` |

The header params are declared `include_in_schema=False`, so they never leak into the harvested OpenAPI spec. **Requires an env rebuild** (new SDK code). The producer building guide `core/prompts/REST_API_BUILDING.md` documents the accessor and the `scopes:` syntax.

### React Query keys (caller identity)

- `["agentApiGrants", agentId]` — the producer's per-user grant list (Access & Scopes card)
- `["agentApiScopeCatalog", agentId]` — the `policy.yaml` scope catalog for the picker

## Test Coverage

`backend/tests/api/agents/agents_agent_api_test.py` — 31 scenario-based API tests. Tokens are minted via the connect helper (the raw token is read back from the created credential's data, exactly as a consumer obtains it); "revoke" = delete the credential. Covered:

- Toggle gates routes (404 when disabled); `_status` reports `disabled` regardless
- Connect mints a token + creates an `agent_api` credential (prefix + base_url + spec_url); raw token readable only from the credential's decrypted data
- Connection lifecycle: connect → token authenticates a consumer call → delete credential → token cascade-deleted → 401
- Connection-info endpoint reports producer + consumers + `read_only`; non-owner → 404
- Producer connections list: empty → linked (with consumer `ui_color_preset` + `owner_name` + `owner_email`) → unlinked; disconnect via `DELETE /connections/{token_id}` revokes the token (401) and drops the row; non-owner → 404
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

`backend/tests/api/agents/agents_agent_api_grants_test.py` — 15 scenario-based tests for caller identity + producer scopes. Covered:

- Grant CRUD lifecycle (create → list → update scopes → delete)
- Create grant to a phantom user → 404
- Grant routes owner-gated, no existence leak (non-owner → 404)
- Scope sanitization on write (whitespace/empties/dupes dropped)
- Scope catalog surfaces scopes declared in `policy.yaml`
- Proxy injects authoritative `X-Cinna-Caller-*` headers and strips the consumer bearer + raw identity token
- Proxy strips inbound forged `X-Cinna-Caller-*` headers
- Missing identity token ⇒ anonymous (no caller headers)
- Invalid/expired identity token ⇒ anonymous
- Scopes injected only when `agent_api_identity_enabled` AND a grant exists
- Edge enforcement gates a scope-required endpoint (403 without the scope)
- Edge enforcement OFF when identity disabled
- Edge enforcement skips documentation-only scopes (no `requires:`)
- Edge enforcement path match is segment-accurate
- Identity token minted via the credential pipeline round-trips to the owner

**In-container coverage gap:** the riskiest code — `AgentApiSupervisor`, `cinna_api` discovery/spec-harvest, `policy.yaml` parsing inside env-core — runs inside the container and is not reachable by the API-only backend suite (the backend tests use `EnvironmentTestAdapter` stubs). This code is covered by manual/env-core verification. The API-only list above covers only the backend proxy edge.

---

*Last updated: 2026-06-21*
