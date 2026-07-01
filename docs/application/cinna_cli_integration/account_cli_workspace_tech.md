# Account CLI Workspace — Technical Details

## File Locations

### Backend — Models (Phase 3 additions)

- `backend/app/models/cli/account_convenience.py` — New file (Phase 3 request schemas, no `table=True`):
  - `AccountAgentCreateBody` — `{name: str, description: str|None, env_name: str|None, user_workspace_id: uuid|None}`. `env_name` accepted-but-noop in v1 (O1 — normal create path hard-codes `DEFAULT_AGENT_ENV_NAME`). `user_workspace_id` targets the account user's **active workspace** (the CLI fills it from `.cinna/account.json`; `None` = Default); validated to belong to the account user before assignment (`WorkspaceNotFoundError` → 404). Credentials created by the connect verbs inherit the agent's workspace automatically, so this one field realizes the "create in my active workspace" intent for both agents and their connection credentials.
  - `AccountConnectAgentApiBody` — `{producer_agent_id: uuid, consumer_agent_id: uuid|None, credential_label: str|None, read_only_override: bool=False}`
  - `AccountConnectMcpBody` — `{connector_id: uuid, consumer_agent_id: uuid|None, mcp_mode_conversation: bool=True, mcp_mode_building: bool=True, label: str|None}`
  - `AccountAgentApiEnableBody` — `{agent_id: uuid, enabled: bool=True}`. Toggles the producer's `agent_api_enabled` (the `cinna agent-api enable [--disable]` verb).
  - `AccountAgentApiRefreshBody` — `{agent_id: uuid}`. Forces a spec + policy re-harvest (the `cinna agent-api refresh` verb). The `spec` read takes `agent_id` as a query param, no body model.
  - `AccountCredentialCreateBody` — `{name: str, type: CredentialType, notes?, service_uri?, allow_sharing: bool=False, user_workspace_id: uuid|None}`. **No `credential_data`** by design — the account CLI creates *drafts*; the user fills the secret in the UI.
  - `AccountCredentialUpdateBody` — `{name?, notes?, service_uri?, allow_sharing?, allow_template_sharing?}`. Metadata only; **no `credential_data`**. All optional (`exclude_unset` applied).
  - `AccountCredentialShareBody` — `{agent_id: uuid}`. Attaches an existing credential to an owned agent.
  - `AccountCredentialDraftResult` — `{credential: CredentialPublic, required_fields: list[str], setup_url: str}`. The create response — the draft plus what the user must fill and where.
  - `AccountCredentialTypeInfo` — `{type: CredentialType, required_fields: list[str], note: str|None}`; `AccountCredentialTypesPublic` — `{data, count}`.
  - `AccountApiProxyRequest` — `{method: Literal[GET,POST,PUT,PATCH,DELETE], path: str, query: dict|None, json_body: Any|None, headers: dict|None}`. `method` validated as uppercase via `field_validator`. `headers` accepted-but-ignored in v1 (O3 — only the minted user JWT is sent inward). Reuses existing response models (`AgentPublic`, `ConnectAgentApiResponse`, `MCPProviderConnectionResponse`, `DiscoverableAgents`); escape-hatch response is a raw `fastapi.Response` passthrough.
  - `AccountStatusRefreshCommandBody` — `{command: str|None}` (max 1024) (Phase 5). Body for `cinna agent status set-command`. `None`/empty = deliberate opt-out.
  - `AccountAgentStatusResult` — `{status: AgentStatusPublic, status_refresh_command: str|None}` (Phase 5). The combined status read — the `STATUS.md` snapshot plus the configured pre-command, returned by both the status get and the set-command verbs. (Imports `AgentStatusPublic` from `agents/agent_status.py`.)
  - **Schedule verbs reuse existing schedule models** — no new request/response models. `ScheduleRequest`, `ScheduleResponse`, `CreateScheduleRequest`, `UpdateScheduleRequest`, `AgentSchedulePublic`, `AgentSchedulesPublic` (from `models/agents/agent_schedule.py`) and `AgentScheduleLogsPublic` (from `models/agents/agent_schedule_log.py`); run-now returns `Message`.

### Backend — Models (Phases 1–2)

- `backend/app/models/cli/cli_token.py` — Extended with:
  - `CLIToken.token_type` (`str`, default `"cli"`, indexed)
  - `CLIToken.minted_by_account_token_id` (`uuid | None`, self-FK → `cli_token.id`, `ON DELETE CASCADE`, indexed)
  - `CLIToken.agent_id` made nullable (`uuid | None`) for account tokens
  - `CLIAccountTokenPublic` — public projection of an account token row, adds `child_count: int`
  - `CLIAccountTokensPublic` — `{data: list[CLIAccountTokenPublic], count: int}`
  - `CLITokenPayload` extended: `agent_id: str | None`, `token_type: str = "cli"`
- `backend/app/models/cli/cli_setup_token.py` — Extended with:
  - `CLISetupToken.kind` (`str`, default `"agent"`, `"account"` for account setup tokens)
  - `CLISetupToken.agent_id` made nullable (`uuid | None`)
  - `CLISetupTokenCreated.agent_id` is now `uuid | None`
- `backend/app/models/cli/account_agent.py` — New file:
  - `AccountAgentListItem` — minimal projection per row in `GET /account/agents`
  - `AccountAgentsPublic` — `{data: list[AccountAgentListItem], count: int}`

### Backend — Routes (Phase 3 additions)

- `backend/app/api/routes/cli.py` — Phase 3 routes appended to the existing account CLI section:
  - `GET /api/v1/cli/account/user-workspaces` — `AccountCLIContextDep`; response `UserWorkspacesPublic`; delegates to `AccountCLIService.list_user_workspaces`. Account-token-reachable catalogue of the user's own workspaces for `cinna account user-workspace list` / `--activate` validation. No `require_developer` (a read of one's own workspaces). The *active* workspace is a client-side setting in `.cinna/account.json`; there is no server-side active-workspace state.
  - `POST /api/v1/cli/account/agents` — `AccountCLIContextDep` + `require_developer`; body `AccountAgentCreateBody`; response `AgentPublic`; delegates to `AccountCLIService.create_agent`. A non-null `user_workspace_id` is ownership-validated (`WorkspaceNotFoundError` → 404, existence-leak discipline) and threaded into `AgentCreate`.
  - `POST /api/v1/cli/account/connect/agent-api` — `AccountCLIContextDep` + `require_developer`; body `AccountConnectAgentApiBody`; response `ConnectAgentApiResponse`; maps `AgentApiTokenError` to its `status_code`. Status codes: 200 / 400 / 403 / 404.
  - `GET /api/v1/cli/account/connect/mcp/discoverable` — `AccountCLIContextDep`; query `?consumer_agent_id=`; response `DiscoverableAgents`. No `require_developer` (listing is unrestricted for account token holders).
  - `POST /api/v1/cli/account/connect/mcp` — `AccountCLIContextDep` + `require_developer`; body `AccountConnectMcpBody`; response `MCPProviderConnectionResponse`; maps `MCPProviderError` to its `status_code`. Status codes: 200 / 400 / 403 / 404.
  - `POST /api/v1/cli/account/agent-api/enable` — `AccountCLIContextDep` + `require_developer`; body `AccountAgentApiEnableBody`; response is the agent-api status dict; maps `AgentApiError` to its `status_code`. Status codes: 200 / 401 / 403 / 404.
  - `POST /api/v1/cli/account/agent-api/refresh` — `AccountCLIContextDep`; body `AccountAgentApiRefreshBody`; response is the agent-api status dict (never raises on a harvest failure — `last_error` carries it); maps `AgentApiError` to its `status_code`. Status codes: 200 / 401 / 404.
  - `GET /api/v1/cli/account/agent-api/spec` — `AccountCLIContextDep`; query `?agent_id=`; response is the harvested OpenAPI spec JSON; maps `AgentApiError` to its `status_code`. Status codes: 200 / 400 (disabled) / 401 / 404 / 503 (env not running + cold cache).
  - `POST /api/v1/cli/account/agent-api/call` — `AccountCLIContextDep`; body `AccountAgentApiCallBody`; response `AccountAgentApiCallResult` (`{status_code, headers, body, is_json}`). Owner-side endpoint smoke test via the owner-preview proxy (`adapter.proxy_agent_api`, buffered `stream=False`, query forwarded); maps `AgentApiError` to its `status_code` (incl. 502 on a proxy transport error). Diagnostic, not audited. Status codes: 200 / 400 (disabled) / 401 / 404 / 502 / 503.
  - `POST /api/v1/cli/account/agents/{agent_id}/restart-env` — `AccountCLIContextDep`; path `agent_id`; response `AccountRestartEnvResult` (`{environment_id, status, status_message}`). `AgentService.assert_can_build` gate; wraps `EnvironmentService.restart_environment` (blocks until restarted); maps `CanBuildError` (404/403), `AgentApiError` / `AgentEnvironmentError` (their `status_code`), `ValueError` → 400 (no active env). **Audited** (`CLI_ACCOUNT_ENV_RESTARTED`). Status codes: 200 / 400 / 401 / 403 / 404.
  - `GET /api/v1/cli/account/agents/{agent_id}/inspect` — `AccountCLIContextDep`; path `agent_id`; response `AccountAgentInspectResult` (prompts from the `Agent` DB fields, feature flags, connected credential **name + type only** via `CredentialsService.get_agent_credentials`, live agent-api status when enabled). Ownership-checked (404 no-leak); diagnostic, not audited. Status codes: 200 / 401 / 404.
  - `POST /api/v1/cli/account/api-proxy` — `AccountCLIContextDep`; body `AccountApiProxyRequest`; raw `Response` passthrough (no `response_model`); maps `ApiProxyDenied` → 403 (`excluded_*`) or 400 (`malformed_path`); 413 (body), 429 (rate limit), 502 (streaming/oversize). Status codes: 200/4xx-5xx (inner) / 400 / 403 / 413 / 429 / 502.

  **Schedule management (Phase 5)** — thin wrappers over `AgentSchedulerService`, nested under `/account/agents/{agent_id}/schedules`. Ownership is enforced 404-no-leak via `AccountCLIService._resolve_owned_agent` (→ `AgentApiError`); the foreign-install read-only contract matches the UI route. Routes catch `AgentApiError` (ownership) and `ScheduleError` (the scheduler service's status-coded exception). A module-level `_ACCOUNT_SCHEDULE_RUN_MESSAGES` maps the `ManualRunResult.action` → message (copy stays in the route, mirroring `agents.py::_RUN_NOW_MESSAGES`):
  - `GET /api/v1/cli/account/agents/{agent_id}/schedules` — `AccountCLIContextDep`; response `AgentSchedulesPublic`; read (no `require_developer`).
  - `POST /api/v1/cli/account/agents/{agent_id}/schedules/generate` — `AccountCLIContextDep`; body `ScheduleRequest`; response `ScheduleResponse`; stateless AI preview (no `require_developer`).
  - `POST /api/v1/cli/account/agents/{agent_id}/schedules` — `AccountCLIContextDep` + `require_developer`; body `CreateScheduleRequest`; response `AgentSchedulePublic`. Foreign install → 403; `script_trigger` without command / unknown type / too-frequent cadence → 400. Emits `CLI_ACCOUNT_SCHEDULE_CREATED`.
  - `PUT /api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}` — `AccountCLIContextDep` + `require_developer`; body `UpdateScheduleRequest`; response `AgentSchedulePublic`. On a foreign install only `enabled` may change (any other set field → 403). Emits `CLI_ACCOUNT_SCHEDULE_UPDATED`.
  - `DELETE /api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}` — `AccountCLIContextDep` + `require_developer`; response `Message`. Foreign install → 403. Emits `CLI_ACCOUNT_SCHEDULE_DELETED`.
  - `POST /api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/run` — `AccountCLIContextDep` + `require_developer`; response `Message` (env-state-aware). Allowed on foreign installs. Emits `CLI_ACCOUNT_SCHEDULE_RUN`.
  - `GET /api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/logs` — `AccountCLIContextDep`; response `AgentScheduleLogsPublic`; read (no `require_developer`).

  **Status management (Phase 5)** — thin wrappers over `AgentStatusService` + `AgentService`, nested under `/account/agents/{agent_id}/status`. Ownership 404-no-leak via `_resolve_owned_agent`:
  - `GET /api/v1/cli/account/agents/{agent_id}/status?force_refresh=bool` — `AccountCLIContextDep`; response `AccountAgentStatusResult` (snapshot + `status_refresh_command`). `force_refresh=true` runs the full `force_refresh_status` flow (wake suspended env → pre-command → live fetch → cache fallback; never raises). Read — not audited.
  - `POST /api/v1/cli/account/agents/{agent_id}/status/refresh-command` — `AccountCLIContextDep` + `require_developer`; body `AccountStatusRefreshCommandBody`; response `AccountAgentStatusResult`. Flips `status_refresh_command` via `AgentService.update_agent`. Emits `CLI_ACCOUNT_STATUS_COMMAND_SET`.

  **Knowledge search:**
  - `async search_knowledge(db, user, query, topic=None) -> list[dict]` — account-level analogue of the per-agent knowledge search. No `require_developer` (read). Delegates to `CLIService.search_user_knowledge` with `workspace_id=None`. No SecurityEvent / audit (high-frequency read; mirrors the unaudited per-agent route).

  **Console-chat file upload (Phase 5):**
  - `POST /api/v1/cli/account/files/upload` — `AccountCLIContextDep`; multipart `UploadFile` body; `response_model=FileUploadPublic`. Dedicated multipart route needed because the JSON-only api-proxy cannot carry a binary `multipart/form-data` body. Implemented in `backend/app/api/routes/cli.py` (function `account_upload_file`). Delegates to `FileService.create_file_upload(session=db, user_id=account_ctx.user.id, file=file)` — the same service the normal `POST /files/upload` uses, so it inherits the same size cap, MIME-type whitelist, and per-user storage quota validation. New uploads start with `status="temporary"` and become durable when referenced in a session message's `file_ids`. Returns `FileUploadPublic` (`id`, `filename`, `file_size`, `mime_type`, `status`, `uploaded_at`). Status codes: 200 / 400 (oversize / invalid MIME / quota exceeded) / 401. No new model, no migration, no config knob. No SecurityEvent — an authenticated account-user file upload is equivalent to the normal upload route's audit surface.

  **Session control plane via api-proxy (Phase 5 / `cinna chat`):**
  The session routes are NOT on `EXCLUDED_PREFIXES`, so `cinna chat` reaches all of them through the existing `POST /account/api-proxy` escape hatch — no new session routes were added:
  - `POST sessions/` — create a session
  - `GET sessions/{id}` — fetch session metadata
  - `POST sessions/{id}/messages/stream` — send a message; returns a JSON ack dict (`MessageService.build_stream_response`), **not** an SSE body (real-time streaming is over socket.io, unavailable to the CLI). The api-proxy would block a genuine `text/event-stream` response (→ 502), but this endpoint returns JSON so the proxy delivers it normally.
  - `GET sessions/{id}/messages` — poll for reply messages
  - `GET sessions/{id}/messages/streaming-status` — poll for streaming progress
  - `POST sessions/{id}/messages/interrupt` — cancel a running generation
  - `GET files/{id}/download` — retrieve files the agent attaches in its reply; the proxy mirrors binary response bodies 1:1 (bounded by the 8 MiB `ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES` cap).

  **Credential drafting verbs** (metadata + structure only — the account token never reads or writes a credential's secret value; these expose the *safe* slice of an otherwise denylisted surface):
  - `GET /api/v1/cli/account/credentials/types` — `AccountCLIContextDep`; response `AccountCredentialTypesPublic`; static catalogue of `CredentialType` + per-type `required_fields` (from `CredentialsService.REQUIRED_FIELDS`). No `require_developer` (read).
  - `GET /api/v1/cli/account/credentials` — `AccountCLIContextDep`; query `?user_workspace_id=` (omitted = all, `""` = Default, UUID = that workspace); response `CredentialsPublic` (metadata only via the same projection as the credentials route — `share_count` + computed `status`, **never `credential_data`**). No `require_developer`.
  - `POST /api/v1/cli/account/credentials` — `AccountCLIContextDep` + `require_developer`; body `AccountCredentialCreateBody` (**no `credential_data` field**); response `AccountCredentialDraftResult` (`{credential: CredentialPublic, required_fields, setup_url}`). Creates an empty draft (`status="incomplete"`). 404 if `user_workspace_id` not owned. Emits `CLI_ACCOUNT_CREDENTIAL_CREATED`.
  - `PUT /api/v1/cli/account/credentials/{credential_id}` — `AccountCLIContextDep` + `require_developer`; body `AccountCredentialUpdateBody` (**metadata only — no `credential_data`**); response `CredentialPublic`; maps service `ValueError` → 404/400. Emits `CLI_ACCOUNT_CREDENTIAL_UPDATED`.
  - `DELETE /api/v1/cli/account/credentials/{credential_id}` — `AccountCLIContextDep` + `require_developer`; query `?force=`; response `Message`; reuses `CredentialsService.delete_credential` blast-radius gate (409 with `CredentialDeletionImpact` on Tier 2 unless force); `ValueError` → 404/400. Emits `CLI_ACCOUNT_CREDENTIAL_DELETED`.
  - `POST /api/v1/cli/account/credentials/{credential_id}/share-with-agent` — `AccountCLIContextDep` + `require_developer`; body `AccountCredentialShareBody {agent_id}`; response `Message`; delegates to `CredentialsService.link_credential_to_agent` (non-owned agent → 400 "Not enough permissions"; missing agent → 404, mirroring the UI `POST /agents/{id}/credentials` mapping). Emits `CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT`.
  - Helper `_require_developer_account(account_ctx)` — local helper that calls `RoleService.require_developer(account_ctx.user)` and maps `PermissionError` → 403. Mirrors the pattern used by `create_account_setup_token`.

### Backend — Routes (Phases 1–2)

- `backend/app/api/routes/cli.py` — Extended with:
  - `GET /api/cli-setup/account/{token}` — serve account bootstrap script
    (calls `CLIService.render_bootstrap_script(token, request, flavor="account")`)
  - `POST /api/cli-setup/account/{token}` — exchange account setup token
  - `POST /api/v1/cli/account/setup-tokens` — create account setup token
    (`require_developer` gated via `RoleService`)
  - `GET /api/v1/cli/account/tokens` — list account tokens
  - `DELETE /api/v1/cli/account/tokens/{token_id}` — revoke account token
    (cascade)
  - `GET /api/v1/cli/account/context-package` — download the orchestrator
    context package as a gzip tarball (authenticated via `AccountCLIContextDep`);
    delegates to `ContextPackageService.get_context_package()`; returns
    `StreamingResponse` with `Content-Type: application/tar+gzip` and
    `Content-Disposition: attachment; filename="context-package.tar.gz"`;
    **503** if the platform snapshot is missing from this deployment
  - `GET /api/v1/cli/account/agents` — list accessible agents
    (authenticated via `AccountCLIContextDep`)
  - `POST /api/v1/cli/account/agents/{agent_id}/mint` — mint child token
    (authenticated via `AccountCLIContextDep`)
  - `DELETE /api/v1/cli/account/tokens/children/{child_token_id}` — revoke a
    single child token minted by the calling account token (`cinna agent
    unsync`); authenticated via `AccountCLIContextDep`; 200 on success,
    idempotent on already-revoked (no duplicate security event), 404 for any
    token not owned by this account token (existence-leak discipline), 401 for
    user JWTs or per-agent tokens
  - Helper `_raise_can_build_http(e)` — maps `CanBuildError.reason` to 404
    (`not_accessible`) or 403 (all other reasons)

### Backend — Services (Phase 3 additions)

- `backend/app/services/cli/account_api_proxy_policy.py` — Pure, dependency-free exclusion chokepoint (mirrors `assert_url_allowed` egress guard pattern):
  - `ApiProxyDenied(reason, message)` — `reason ∈ {"excluded_path", "excluded_method", "malformed_path"}`.
  - `assert_api_proxy_allowed(method: str, normalized_path: str) -> None` — the ONE gate for the escape hatch. Called once per proxy request; never called from any other site. Raises `ApiProxyDenied` for excluded targets.
  - `EXCLUDED_PREFIXES` — tuple of path-segment prefixes denied from the escape hatch (without `/api/v1` prefix; `_excluded_prefixes()` prepends it at call time). Matched by `_segment_prefix_match` which requires a segment boundary (`/api/v1/users` matches `users`; `/api/v1/users-public` does NOT).
  - `USER_PATH_ALLOW_EXACT` — exact `(method, path)` pairs carved back in from the user exclusion: `("GET", "users/me")` and `("GET", "users/search")`.
  - `STREAMING_DENY` — path-segment prefixes for streaming/create-flow routes denied regardless of method.
  - `ALLOWED_METHODS` — `frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})`.
  - `_SAFE_PATH_RE` — `^/api/v1(?:/[A-Za-z0-9._~%{}-]+)*/?$` — shape guard re-asserted defensively inside `assert_api_proxy_allowed` after the caller's own normalization.

- `backend/app/services/cli/account_api_proxy_service.py` — `AccountApiProxyService`:
  - `_RateLimiter` — in-memory sliding-window throttle keyed by account-token id (`threading.Lock` protected). Process-local; backstop against runaway agent loops, not a billing control.
  - `_normalize_path(raw_path: str) -> str` — normalizes caller-supplied path to `/api/v1/<...>`: strips `?`/`#`/`\`, rejects `..`, strips leading `/api/v1` the caller may have included, collapses double slashes. Raises `ApiProxyDenied(malformed_path)`.
  - `async proxy(db, account_token, user, req, request) -> Response` — main entry point: (1) rate-limit check → 429; (2) normalize + `assert_api_proxy_allowed`; (3) request body size → 413; (4) mint 8s user JWT via `create_access_token(user.id, expires_delta=timedelta(seconds=8))` — never returned to CLI; (5) `httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app))` in-process re-dispatch; (6) trailing-slash 307/308 followed with chokepoint re-assert; (7) `text/event-stream` guard → 502; response size → 502; (8) buffered `Response` with forwarded-header allowlist (`content-type`, `content-disposition`).
  - `async _audit_denied(...)` — writes `CLI_ACCOUNT_API_PROXY_CALL` SecurityEvent on `excluded_*` reasons only (not `malformed_path`).
  - Internal constants: `_INNER_JWT_TTL_SECONDS = 8`, `_INNER_DISPATCH_TIMEOUT_SECONDS = 30.0`, `_FORWARDED_RESPONSE_HEADERS = ("content-type", "content-disposition")`, `_INTERNAL_BASE_URL = "http://internal"`.

- `backend/app/services/cli/account_cli_service.py` — Extended (Phase 3) with:
  - `async create_agent(db, user, body, request) -> Agent` — resolves `body.user_workspace_id` via `_resolve_owned_workspace_id`, then maps `AccountAgentCreateBody` → `AgentCreate(name, description, user_workspace_id=<resolved>)` and delegates to `AgentService.create_agent`. No Phase 3 SecurityEvent (agent creation is already audited on the normal path).
  - `_resolve_owned_workspace_id(db, user, workspace_id) -> uuid|None` — returns `None` for the Default workspace; for a non-null id, looks it up via `UserWorkspaceService.get_workspace` and asserts `user_id` ownership, else raises `WorkspaceNotFoundError` (route → 404). The single guard that keeps a CLI-created agent from being assigned into a foreign/invisible workspace.
  - `list_user_workspaces(db, user) -> UserWorkspacesPublic` — projects the user's own workspaces (`UserWorkspaceService.get_user_workspaces`) into `UserWorkspacePublic` rows. Supplies the catalogue for `cinna account user-workspace`; the active selection itself is client-side (no server state).
  - `WorkspaceNotFoundError` — module-level exception raised by `_resolve_owned_workspace_id`; the create route maps it to 404.
  - `async connect_agent_api(db, user, body, request) -> ConnectAgentApiResponse` — maps `AccountConnectAgentApiBody` → `ConnectAgentApiRequest` and delegates to `AgentApiTokenService.connect_agent_api`. Emits `CLI_ACCOUNT_CONNECT_AGENT_API` on success.
  - `list_discoverable_mcp_agents(db, user, consumer_agent_id) -> DiscoverableAgents` — delegates to `MCPProviderService.list_discoverable_agents`.
  - `async connect_mcp(db, user, body, request) -> MCPProviderConnectionResponse` — maps `AccountConnectMcpBody` → `ConnectMcpProviderAgentRequest` and delegates to `MCPProviderService.connect_to_agent`. Emits `CLI_ACCOUNT_CONNECT_MCP` on success.
  - **Agent REST API producer management** (the producer-side build+verify half that precedes `connect_agent_api`):
    - `_resolve_agent_api_env(db, agent) -> AgentEnvironment|None` — the agent's active environment (or `None` when suspended/absent), shared by `set_agent_api_enabled` / `refresh_agent_api`.
    - `async set_agent_api_enabled(db, user, agent_id, enabled, request) -> dict` — ownership-checks via `AgentApiService.resolve_agent_only` (404 no-leak), flips `agent_api_enabled` through `AgentService.update_agent(AgentUpdate(...))` (same path as the UI `PUT /agents/{id}`), emits `CLI_ACCOUNT_AGENT_API_ENABLED`, and returns `AgentApiService.get_status`.
    - `async refresh_agent_api(db, user, agent_id, request) -> dict` — ownership-checks, then (when enabled + env running) best-effort `AgentApiService.get_spec(force_refresh=True)` + `load_policy(force_refresh=True)` and returns `get_status`. Mirrors the producer `POST /_refresh`; never raises on a harvest failure (the error is persisted and surfaced via the status `last_error`). Not audited (diagnostic, not a grant).
    - `async get_agent_api_spec(db, user, agent_id) -> dict` — `AgentApiService.resolve_producer_environment(require_agent_api_enabled=True)` then `get_spec`; bumps `update_last_activity` to keep the env warm. Mirrors the owner `GET /openapi.json`.
  - **Credential drafting verbs** (secrets-safe — never read/write `credential_data`):
    - `_credential_public(db, credential) -> CredentialPublic` — owner projection (share_count + computed `status`); decrypts only to compute completeness server-side, plaintext never leaves the function.
    - `_required_fields_for(credential_type) -> list[str]` — per-type required fields from `CredentialsService.REQUIRED_FIELDS`.
    - `list_credentials(db, user, user_workspace_id) -> CredentialsPublic` — owner-scoped, standard workspace-filter semantics; metadata only.
    - `list_credential_types() -> AccountCredentialTypesPublic` — static catalogue (notes the api_token custom-variant conditional).
    - `async create_credential_draft(db, user, body, request) -> AccountCredentialDraftResult` — validates workspace via `_resolve_owned_workspace_id`, calls `CredentialsService.create_credential` with `credential_data=None`, emits `CLI_ACCOUNT_CREDENTIAL_CREATED`.
    - `async update_credential_metadata(db, user, credential_id, body, request) -> CredentialPublic` — builds `CredentialUpdate` from provided safe fields only (defensively pops any `credential_data`), delegates to `CredentialsService.update_credential`, emits `CLI_ACCOUNT_CREDENTIAL_UPDATED`.
    - `async delete_credential(db, user, credential_id, force, request)` — delegates to `CredentialsService.delete_credential` (propagates `CredentialInUseError`/`ValueError`), emits `CLI_ACCOUNT_CREDENTIAL_DELETED`.
    - `async share_credential_with_agent(db, user, credential_id, agent_id, request)` — delegates to `CredentialsService.link_credential_to_agent`, emits `CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT`.
  - **Schedule + status management (Phase 5)** — thin delegators over `AgentSchedulerService` / `AgentStatusService` / `AgentService`:
    - `_resolve_owned_agent(db, user, agent_id) -> Agent` — shared 404-no-leak ownership resolve (delegates to `AgentApiService.resolve_agent_only`); used by every schedule + status verb.
    - `_guard_foreign_schedule_write(agent)` — raises `ScheduleError(..., 403)` on a foreign (bundle-consumer) install; mirrors the UI route guard.
    - `list_schedules` / `generate_schedule` / `get_schedule_logs` — sync reads (resolve ownership, delegate to `AgentSchedulerService.get_agent_schedules` / `generate_schedule_preview` / `get_schedule_logs`). Not audited.
    - `async create_schedule(db, user, agent_id, body, request)` — foreign-guard + type/command validation (same as the UI route), delegate to `AgentSchedulerService.create_schedule`, emit `CLI_ACCOUNT_SCHEDULE_CREATED`.
    - `async update_schedule(db, user, agent_id, schedule_id, body, request)` — `get_schedule_for_agent` (404), foreign-install enabled-only guard, delegate to `AgentSchedulerService.update_schedule(**exclude_unset)`, emit `CLI_ACCOUNT_SCHEDULE_UPDATED`.
    - `async delete_schedule(db, user, agent_id, schedule_id, request)` — foreign-guard, `get_schedule_for_agent`, `delete_schedule`, emit `CLI_ACCOUNT_SCHEDULE_DELETED`.
    - `async run_schedule(db, user, agent_id, schedule_id, request) -> ManualRunResult` — `get_schedule_for_agent`, `execute_now`, emit `CLI_ACCOUNT_SCHEDULE_RUN`. Foreign installs allowed.
    - `_status_snapshot_to_public(snapshot, agent_id)` — local mirror of `agent_status.py::_snapshot_to_public`.
    - `async get_agent_status(db, user, agent_id, force_refresh) -> AccountAgentStatusResult` — resolves the primary environment; `force_refresh` → `AgentStatusService.force_refresh_status` else `get_cached_status`; returns snapshot + `agent.status_refresh_command`. Not audited.
    - `async set_status_refresh_command(db, user, agent_id, command, request) -> AccountAgentStatusResult` — ownership-check, `AgentService.update_agent(AgentUpdate(status_refresh_command=command))`, emit `CLI_ACCOUNT_STATUS_COMMAND_SET`, return cached status + the new command.

### Backend — Services (Phases 1–2)

- `backend/app/services/cli/context_package_service.py` — `ContextPackageService`:
  builds (and memoizes) the orchestrator context package tarball:
  - `get_context_package()` — public entry point; returns a `StreamingResponse`.
    Calls `_build_or_cached()`; propagates the 503 `HTTPException` from
    `_build_tarball` if the snapshot is absent.
  - `_build_or_cached()` — double-checked lock; cache key from
    `_snapshot_version()`. A 503 raised inside `_build_tarball` is NOT cached,
    so the next request retries rather than serving a permanent failure.
  - `_snapshot_version(platform_dir, examples_dir, guides_dir)` — cache key
    derived from the newest file mtime AND file count across all three snapshot
    source directories (platform, examples, guides); a redeploy or guide edit
    automatically invalidates the cache.
  - `_build_tarball(platform_dir, examples_dir, guides_dir)` — in-memory
    `tarfile.open` (`mode="w:gz"`); raises **503** if `platform_dir` is missing
    or empty; warns + omits the optional trees if absent; adds:
    - `context/platform/<rel>` for every file under `platform_dir/` that is
      NOT under `platform_dir/api_reference/`
    - `context/api_reference/<rel>` for files under `platform_dir/api_reference/`
      (promoted to top level)
    - `context/examples/<rel>` for files under `examples_dir/` (if present;
      warn-and-omit on missing)
    - `context/guides/<rel>` for files under `guides_dir/` (if present;
      warn-and-omit on missing — Phase 4)
    - `context/README.md` — generated package index rendered by `_render_index()`
      (now includes a `guides/` row pointing at `build-an-agentic-network.md`)
  - `_cache: tuple[str, bytes] | None` — process-local class variable; guarded by
    `threading.Lock()` for concurrent requests.

- `backend/app/services/cli/platform_knowledge_assets.py` — shared generation module
  for the platform's self-knowledge snapshot:
  - `platform_knowledge_dir()` — resolves the path to
    `<ENV_TEMPLATES_DIR>/platform-knowledge-env/app/workspace/knowledge/platform/`
    (lazy import of `app.core.config` so the module can be imported by the
    repo-root sync script without triggering backend Settings validation).
  - `example_scripts_dir()` — resolves
    `<ENV_TEMPLATES_DIR>/platform-knowledge-env/app/workspace/scripts/examples/`.
  - `guides_dir()` — resolves
    `<ENV_TEMPLATES_DIR>/platform-knowledge-env/app/workspace/knowledge/guides/`
    (Phase 4). Sibling of `knowledge/platform/`; outside the `sync_platform_knowledge.py`
    rmtree target, so the docs sync never touches it.
  - `generate_api_reference(spec)` — groups OpenAPI paths by tag, skips
    `SKIP_TAGS` (`login`, `oauth`, `private`, `utils`, `items`, `mcp-oauth`,
    `mcp-upload`, `mcp-consent`, `webapp-public`, `webapp-shares`,
    `shared-workspace`, `security-events`), and renders one markdown string
    per remaining tag with method, path, summary, parameters, request body,
    and response type.
  - `api_reference_index(spec, references)` — builds `api_reference/README.md`
    with a table of domain → filename → endpoint count.
  - This module is the single source of truth for both the backend endpoint
    (`context_package_service.py`) and the repo-root sync script
    (`.cinna-core-kit/scripts/sync_platform_knowledge.py`), which imports from it.

- `backend/app/services/cli/cli_service.py` — `CLIService` — **refactored knowledge search core**:
  - `async search_user_knowledge(db, *, user_id, query, topic=None, workspace_id=None) -> list[dict]` — reusable user-scoped knowledge search. Resolves accessible source IDs via `get_accessible_source_ids(user_id, workspace_id)` (public + user-owned private; `workspace_id=None` skips the workspace filter), runs vector search, and returns `[{content, source, similarity}]`. Returns `[]` on `VectorSearchError` or empty source set. Both the per-agent and account-level paths delegate here.
  - `async search_knowledge(db, agent_id, user_id, query, topic=None) -> list[dict]` — per-agent path (unchanged behavior): resolves `workspace_id` from `agent.user_workspace_id` and delegates to `search_user_knowledge`.

- `backend/app/services/cli/account_cli_service.py` — `AccountCLIService`:
  all static methods (mirrors `CLIService` style):
  - `create_account_setup_token(db, user, request)` — creates `CLISetupToken`
    with `agent_id=None, kind="account"`; returns `CLISetupTokenCreated` with
    `setup_command = "curl -sL .../api/cli-setup/account/{token} | python3 -"`
  - `exchange_account_setup_token(db, token_str, machine_name, machine_info, request)`
    — validates `kind=="account"`, creates `CLIToken` with
    `agent_id=None, token_type="cli-account"`, marks setup token used, emits
    `CLI_ACCOUNT_TOKEN_CREATED` security event; returns
    `{account_token, platform_url, frontend_url, machine_name}`
  - `list_accessible_agents(db, user)` — queries `Agent.owner_id == user.id`,
    single-pass environment lookup (no N+1), projects each into
    `AccountAgentListItem` with computed `is_foreign_install` and `can_build`
  - `mint_child_token(db, user, account_token, agent_id, machine_name, machine_info, request)`
    — loads agent (404 if missing), calls
    `AgentService.assert_can_build(db, user, agent)`, creates standard
    `CLIToken` with `token_type="cli"`, `minted_by_account_token_id=account_token.id`,
    emits `CLI_ACCOUNT_CHILD_TOKEN_MINTED` security event; returns per-agent
    exchange-like payload
  - `list_account_tokens(db, user)` — filters non-revoked, non-expired
    `token_type="cli-account"` tokens; counts children per-token
  - `revoke_account_token(db, token_id, user)` — ownership-checked soft-revoke
    of the account token plus all rows where
    `minted_by_account_token_id == token_id`; returns total count revoked

- `backend/app/services/agents/agent_service.py` — Extended with:
  - `AgentService.is_foreign_install(agent)` — `bundle_uuid is not None AND NOT is_publisher_install`
  - `AgentService.user_can_access(session, user, agent)` — `agent.owner_id == user.id`
  - `AgentService.can_build(session, user, agent)` — `is_developer(user) AND NOT is_foreign_install(agent) AND user_can_access(session, user, agent)`
  - `AgentService.assert_can_build(session, user, agent)` — raises `CanBuildError`; checks access first (→ `"not_accessible"`), then role (→ `"not_developer"`), then foreign install (→ `"foreign_install"`)
  - `CanBuildError(reason, message)` — `reason ∈ {"not_developer", "foreign_install", "not_accessible"}`

- `backend/app/services/cli/cli_auth.py` — `CLIAuthService.create_cli_jwt`
  extended with `token_type: str = "cli"` and `agent_id: uuid.UUID | None`
  parameters. `decode_cli_jwt` accepts both `"cli"` and `"cli-account"` types
  (added to `VALID_TOKEN_TYPES`).

### Backend — Dependencies

- `backend/app/api/deps.py` — Extended with:
  - `AccountCLIContext(SQLModel)` — `{user: User, cli_token: Any}`
  - `_resolve_account_cli_context(db, raw_token)` — decodes JWT, **requires**
    `token_type == "cli-account"`, loads `CLIToken`, validates revoked/expired,
    loads active user, calls `CLIAuthService.refresh_token_usage(db, token, environment=None)`
  - `get_account_cli_context(token, db)` — HTTP dep; maps `CLIAuthError` → 401
  - `AccountCLIContextDep = Annotated[AccountCLIContext, Depends(get_account_cli_context)]`
  - `_resolve_cli_context` hardened: rejects `token_type != "cli"` (account
    tokens cannot satisfy per-agent context)

### Backend — Security Events

- `backend/app/models/events/security_event.py`:
  - `CLI_ACCOUNT_TOKEN_CREATED = "CLI_ACCOUNT_TOKEN_CREATED"`
  - `CLI_ACCOUNT_CHILD_TOKEN_MINTED = "CLI_ACCOUNT_CHILD_TOKEN_MINTED"`
  - `CLI_ACCOUNT_CHILD_TOKEN_REVOKED = "CLI_ACCOUNT_CHILD_TOKEN_REVOKED"`
  - `CLI_ACCOUNT_CONNECT_AGENT_API = "CLI_ACCOUNT_CONNECT_AGENT_API"` (Phase 3)
  - `CLI_ACCOUNT_CONNECT_MCP = "CLI_ACCOUNT_CONNECT_MCP"` (Phase 3)
  - `CLI_ACCOUNT_API_PROXY_CALL = "CLI_ACCOUNT_API_PROXY_CALL"` (Phase 3 — exclusion hits only)
  - `CLI_ACCOUNT_CREDENTIAL_CREATED` / `CLI_ACCOUNT_CREDENTIAL_UPDATED` / `CLI_ACCOUNT_CREDENTIAL_DELETED` / `CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT` — one per credential drafting write (create/update/delete/attach). Discrete, infrequent state changes, audited per call (mirrors the connect verbs).
  - `CLI_ACCOUNT_AGENT_API_ENABLED = "CLI_ACCOUNT_AGENT_API_ENABLED"` — written on `agent-api enable` (and `--disable`); `details={enabled, ip}`, `agent_id` = producer. `refresh` / `spec` / `call` are diagnostic and **not** audited (mirrors the unaudited credential *reads*).
  - `CLI_ACCOUNT_ENV_RESTARTED = "CLI_ACCOUNT_ENV_RESTARTED"` — written on `agent restart-env` (a build-rights state change that bounces the container); `details={environment_id, ip}`, `agent_id` = target. `agent show` (inspect) is diagnostic and **not** audited.
  - `CLI_ACCOUNT_SCHEDULE_CREATED` / `CLI_ACCOUNT_SCHEDULE_UPDATED` / `CLI_ACCOUNT_SCHEDULE_DELETED` / `CLI_ACCOUNT_SCHEDULE_RUN` (Phase 5) — one per schedule write (`severity="low"`); `agent_id` = target. `list` / `generate` / `logs` are diagnostic reads and **not** audited.
  - `CLI_ACCOUNT_STATUS_COMMAND_SET` (Phase 5) — written on `agent status set-command`; `details={command, ip}`, `agent_id` = target. `status show` / `refresh` are diagnostic and **not** audited.

### Frontend — Components

- `frontend/src/components/UserSettings/LocalDevelopmentCard.tsx` — Settings
  card:
  - Rendered only when `useRole().isDeveloper` is true
  - React Query key `["account-cli-tokens"]` → `CliService.listAccountTokens()`
  - "Setup" button (header) → `CliService.createAccountSetupToken()`
    mutation; on success stores token in component state and starts a 1-second
    expiry countdown
  - Setup-command section: read-only `Input` + three icon buttons (Regenerate,
    Copy token, Copy command); countdown hidden once expired
  - Active sessions list: styled to match the App Sessions card (`divide-y`
    list, leading `Laptop` icon, name + muted sub-line) — one row per token
    showing machine name, synced-child count ("N agents synced"), and a ghost
    Disconnect icon button (`Unplug`) with an `AlertDialog` that names the count
    in the warning text; `revokeAccountToken` mutation invalidates
    `["account-cli-tokens"]`

### Frontend — Placement

- `frontend/src/routes/_layout/settings.tsx` — `LocalDevelopmentCard` lives in
  the **Security** tab grid (alongside `DesktopSessionsCard`)

### Frontend — Generated Client

- `frontend/src/client/sdk.gen.ts` — `CliService` extended:
  - `createAccountSetupToken()` → `POST /api/v1/cli/account/setup-tokens`
  - `listAccountTokens()` → `GET /api/v1/cli/account/tokens`
  - `revokeAccountToken({ tokenId })` → `DELETE /api/v1/cli/account/tokens/{tokenId}`
- `frontend/src/client/types.gen.ts` — `CLIAccountTokenPublic`,
  `CLIAccountTokensPublic`, `AccountAgentListItem`, `AccountAgentsPublic`

### Migrations

- `backend/app/alembic/versions/5abf2cec7a18_add_account_cli_tokens.py`
  — Single revision, `down_revision = "e8f1a2b3c4d5"`:
  - `cli_setup_token.kind` VARCHAR NOT NULL, `server_default='agent'`
  - `cli_setup_token.agent_id` — DROP NOT NULL
  - `cli_token.token_type` VARCHAR NOT NULL, `server_default='cli'`
  - `cli_token.minted_by_account_token_id` UUID nullable, self-FK
    `cli_token.id` ON DELETE CASCADE
  - `ix_cli_token_minted_by_account_token_id` (btree)
  - `ix_cli_token_token_type` (btree)
  - `cli_token.agent_id` — DROP NOT NULL
  - Downgrade: deletes account rows before re-imposing NOT NULL

### Tests

- `backend/tests/api/cli/test_account_cli.py` — 16 scenario-based API tests
  (Scenarios 1–13: Phase 1 flows; Scenarios 14–16: Phase 2 context package;
  Scenario 14 extended in Phase 4 to also assert `context/guides/`):
  - Setup-token lifecycle (creation, exchange, single-use guard, agent-user 403)
  - Token-type structural isolation (account token rejected on 5 per-agent routes;
    per-agent token rejected on 2 account routes; user JWT rejected on account routes)
  - `can_build` gate on mint (developer-owned standalone → 200; foreign install → 403;
    agent-user can't get account token → 403; inaccessible agent → 404; non-existent → 404)
  - Mint provenance and child token fields (workspace-bootstrap fields, multiple mints
    produce distinct tokens, `child_count` reflects minted tokens)
  - Cascade revoke (account token A revokes its two children; account token B and its
    child unaffected; independently revoking one child leaves siblings alive)
  - Account token management (list, revoke, 404 on non-existent, 404 on another user's token)
  - Account agents listing (`can_build` / `is_foreign_install` flags correct; other users'
    agents not visible; no sensitive fields)
  - Kind guard on setup-token exchange (per-agent token on account path → 400;
    account token on per-agent path → 400)
  - SecurityEvent audit (`CLI_ACCOUNT_TOKEN_CREATED` on exchange;
    `CLI_ACCOUNT_CHILD_TOKEN_MINTED` per mint)
  - Bootstrap script GET route (returns plain-text without token validation)
  - Revoked token rejected on account routes (401 on agents listing and mint)
  - Per-agent setup-token `can_build` regression (foreign install now 403; developer-owned
    exchange still works)
  - Individual child-token revocation via `DELETE /account/tokens/children/{id}` (success
    path, idempotent on already-revoked, 404 for unrelated token, 401 for user JWT)
  - **Scenario 14** — `GET /account/context-package`: valid account token → 200,
    correct `Content-Type: application/tar+gzip`, `Content-Disposition: attachment;
    filename="context-package.tar.gz"`, valid gzip tarball, every member under
    `context/`, `context/README.md` present, at least one file each under
    `context/platform/`, `context/api_reference/`, `context/examples/`; Phase 4
    extends this assertion to also verify `context/guides/build-an-agentic-network.md`
    is present in the tarball; no `..` or absolute-path members
  - **Scenario 15** — Auth matrix for `GET /account/context-package`: missing auth
    → 401; regular user JWT → 401; per-agent child CLI token → 401; revoked account
    token → 401; fresh valid account token → 200
  - **Scenario 16** — In-process cache: two consecutive calls return identical bytes
    (exercises `ContextPackageService._cache` keyed by `_snapshot_version()`); both
    responses are structurally valid tarballs

  - **Scenario 21b** — `POST /account/knowledge/search`: valid account token + query → 200, `{"results": [...]}` shape; optional `topic` param accepted; per-agent CLI token → 401; regular user JWT → 401; revoked account token → 401.

  - **Scenario 22** (`test_account_schedule_management`) — full schedule CRUD via the account token: empty list → generate-preview (AI mocked) → create static_prompt → `script_trigger` without command 400 → list reflects → update toggle → run now (stub env running) → logs → delete → empty list; gating (ghost 404, other user's agent 404 no-leak, user JWT 401, demoted agent-user 403 on write); **foreign (bundle) install → 403 on create** (publish + install a bundle into a fresh developer's account, then assert create is 403).
  - **Scenario 23** (`test_account_agent_status`) — status verbs: cached read → `AccountAgentStatusResult` shape (`status` + `status_refresh_command`); force refresh never raises; set-command updates and echoes the new command; subsequent read reflects it; gating (ghost 404, user JWT 401, demoted agent-user 403 on set-command).

  - **Scenario 24** (`test_account_file_upload`) — happy path: valid account token + multipart file → 200, response matches `FileUploadPublic` shape (`id`, `filename`, `file_size`, `mime_type`, `status="temporary"`, `uploaded_at`). Auth matrix: per-agent CLI token → 401; regular user JWT → 401; missing auth → 401; revoked account token → 401; valid account token → 200. Error cases: invalid MIME type → 400; oversize file → 400.
  - **Scenario 25** (`test_account_chat_flow_proxy_contract`) — asserts that the session routes and `files/{id}/download` are NOT on the proxy denylist via `assert_api_proxy_allowed` (load-bearing: these must never be added to `EXCLUDED_PREFIXES` without breaking `cinna chat`). End-to-end flow through the api-proxy: upload a file via `POST /account/files/upload` → create session via proxy (`POST sessions/`) → send message with `file_ids` via proxy (`POST sessions/{id}/messages/stream`) → assert the stream endpoint returns a JSON ack dict (not `text/event-stream`) → poll messages (`GET sessions/{id}/messages`) → poll streaming-status (`GET sessions/{id}/messages/streaming-status`). Auth matrix for the proxy session calls (same as standard api-proxy auth matrix).

  **Phase 4 additional assertions (agentic-teams escape-hatch reachability):**
  The Phase 3 chokepoint test (`test_account_api_proxy_policy.py`) asserts that
  `GET /agentic-teams`, `POST /agentic-teams`, `POST /agentic-teams/{id}/nodes/`,
  `POST /agentic-teams/{id}/connections/`, and `POST
  /agentic-teams/{id}/connections/{id}/generate-prompt` are **allowed** (not in
  `EXCLUDED_PREFIXES`). These assertions are load-bearing for Phase 4 — agentic-teams
  must never be added to the denylist without breaking the playbook.

## Database Schema Changes

### cli_token (additions)

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `token_type` | VARCHAR(20) | NOT NULL, `server_default='cli'`, indexed | `"cli"` = per-agent; `"cli-account"` = account token |
| `minted_by_account_token_id` | UUID | nullable, FK → `cli_token.id` ON DELETE CASCADE, indexed | Provenance for child-token cascade revoke |
| `agent_id` | UUID | was NOT NULL, now nullable | NULL for account tokens; non-NULL for per-agent tokens |

### cli_setup_token (additions)

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `kind` | VARCHAR(20) | NOT NULL, `server_default='agent'` | `"agent"` = per-agent; `"account"` = account setup token |
| `agent_id` | UUID | was NOT NULL, now nullable | NULL for account setup tokens |

### New Indexes

| Index | Table | Column | Type |
|-------|-------|--------|------|
| `ix_cli_token_token_type` | `cli_token` | `token_type` | btree |
| `ix_cli_token_minted_by_account_token_id` | `cli_token` | `minted_by_account_token_id` | btree |

## API Route Summary

### No-auth bootstrap routes (setup token is the credential)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/cli-setup/account/{token}` | Serve account bootstrap Python script |
| `POST` | `/api/cli-setup/account/{token}` | Exchange account setup token → account CLI token |

Request body for POST:
```json
{ "machine_name": "My MacBook", "machine_info": "optional string" }
```

Response:
```json
{
  "account_token": "<JWT — shown once>",
  "platform_url": "https://...",
  "frontend_url": "https://...",
  "machine_name": "My MacBook"
}
```

### User-JWT-authenticated routes (require_developer where noted)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/cli/account/setup-tokens` | User JWT + `require_developer` | Create account setup token; returns `CLISetupTokenCreated` |
| `GET` | `/api/v1/cli/account/tokens` | User JWT | List active account tokens with child counts |
| `DELETE` | `/api/v1/cli/account/tokens/{token_id}` | User JWT | Cascade-revoke account token; returns revocation count message |

### Account-CLI-token-authenticated routes (`AccountCLIContextDep`)

| Method | Path | Status codes | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/v1/cli/account/context-package` | 200 / 503 | Download orchestrator context package (`application/tar+gzip`); all members under `context/`; 503 if platform knowledge snapshot is missing from this deployment |
| `GET` | `/api/v1/cli/account/user-workspaces` | 200 | List the account user's own workspaces (catalogue for `cinna account user-workspace`); response `UserWorkspacesPublic` |
| `GET` | `/api/v1/cli/account/agents` | 200 | List accessible agents with `can_build` / `is_foreign_install` / `has_active_environment` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/mint` | 200 / 403 / 404 | Mint per-agent child token; 403 / 404 on `can_build` failures |
| `DELETE` | `/api/v1/cli/account/tokens/children/{child_token_id}` | 200 / 401 / 404 | Revoke a child token minted by this account token (`cinna agent unsync`); idempotent on already-revoked; 404 for any token not provenance-matched to the calling account token |
| `POST` | `/api/v1/cli/account/agents` | 200 / 403 / 404 | Create agent (thin client); `require_developer`-gated; body `AccountAgentCreateBody`; response `AgentPublic`; 404 if `user_workspace_id` is not owned by the caller |
| `POST` | `/api/v1/cli/account/connect/agent-api` | 200 / 400 / 403 / 404 | Wire consumer → producer REST API; `require_developer`-gated; body `AccountConnectAgentApiBody`; response `ConnectAgentApiResponse` |
| `GET` | `/api/v1/cli/account/connect/mcp/discoverable` | 200 | List platform agents exposing an agent2agent connector visible to the caller; query `?consumer_agent_id=` |
| `POST` | `/api/v1/cli/account/connect/mcp` | 200 / 400 / 403 / 404 | Wire consumer → producer MCP connector; `require_developer`-gated; body `AccountConnectMcpBody`; response `MCPProviderConnectionResponse` |
| `POST` | `/api/v1/cli/account/agent-api/enable` | 200 / 401 / 403 / 404 | Toggle producer `agent_api_enabled`; `require_developer`-gated; body `AccountAgentApiEnableBody`; response = agent-api status dict |
| `POST` | `/api/v1/cli/account/agent-api/refresh` | 200 / 401 / 404 | Force a spec + policy re-harvest; body `AccountAgentApiRefreshBody`; response = agent-api status dict (never raises on harvest failure) |
| `GET` | `/api/v1/cli/account/agent-api/spec` | 200 / 400 / 401 / 404 / 503 | Harvested OpenAPI spec; query `?agent_id=`; 400 if disabled, 503 if env not running + cold cache |
| `POST` | `/api/v1/cli/account/agent-api/call` | 200 / 400 / 401 / 404 / 502 / 503 | Owner-side endpoint smoke test (query forwarded); body `AccountAgentApiCallBody`; response `AccountAgentApiCallResult` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/restart-env` | 200 / 400 / 401 / 403 / 404 | Restart the agent env (`can_build`-gated, audited); response `AccountRestartEnvResult` |
| `GET` | `/api/v1/cli/account/agents/{agent_id}/inspect` | 200 / 401 / 404 | Effective prompts / features / connected-credential metadata (no secrets); response `AccountAgentInspectResult` |
| `GET` | `/api/v1/cli/account/credentials/types` | 200 | Credential-type catalogue + per-type `required_fields`; response `AccountCredentialTypesPublic` |
| `GET` | `/api/v1/cli/account/credentials` | 200 | List the user's credentials (metadata only, `status` per row); `?user_workspace_id=` filter; response `CredentialsPublic` |
| `POST` | `/api/v1/cli/account/credentials` | 200 / 403 / 404 | Create a draft credential (no value); `require_developer`-gated; body `AccountCredentialCreateBody`; response `AccountCredentialDraftResult`; 404 on foreign workspace |
| `PUT` | `/api/v1/cli/account/credentials/{credential_id}` | 200 / 400 / 403 / 404 | Update metadata only; `require_developer`-gated; body `AccountCredentialUpdateBody`; response `CredentialPublic` |
| `DELETE` | `/api/v1/cli/account/credentials/{credential_id}` | 200 / 400 / 403 / 404 / 409 | Delete (blast-radius tier-gated; 409+impact on Tier 2 unless `?force=true`); `require_developer`-gated; response `Message` |
| `POST` | `/api/v1/cli/account/credentials/{credential_id}/share-with-agent` | 200 / 400 / 403 / 404 | Attach credential to an owned agent; `require_developer`-gated; body `AccountCredentialShareBody`; response `Message` |
| `POST` | `/api/v1/cli/account/knowledge/search` | 200 / 401 | Search knowledge sources accessible to the account user; body `KnowledgeSearchBody {query, topic?}`; response `{results: [{content, source, similarity}]}`; empty list when no accessible sources; no `require_developer` (read); no audit |
| `POST` | `/api/v1/cli/account/files/upload` | 200 / 400 / 401 | Upload a file (multipart `UploadFile`); response `FileUploadPublic`; delegates to `FileService.create_file_upload`; inherits normal upload's size cap / MIME whitelist / quota; new uploads start `status="temporary"`; no audit event |
| `POST` | `/api/v1/cli/account/api-proxy` | inner / 400 / 403 / 413 / 429 / 502 | Generic escape hatch; body `AccountApiProxyRequest`; raw `Response` passthrough |
| `GET` | `/api/v1/cli/account/agents/{agent_id}/schedules` | 200 / 401 / 404 | List schedules; response `AgentSchedulesPublic` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/schedules/generate` | 200 / 401 / 404 | NL → cron preview (stateless); body `ScheduleRequest`; response `ScheduleResponse` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/schedules` | 200 / 400 / 401 / 403 / 404 | Create schedule; `require_developer`; foreign install → 403; body `CreateScheduleRequest` |
| `PUT` | `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}` | 200 / 400 / 401 / 403 / 404 | Update / toggle; `require_developer`; foreign install enabled-only (else 403); body `UpdateScheduleRequest` |
| `DELETE` | `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}` | 200 / 401 / 403 / 404 | Delete; `require_developer`; foreign install → 403; response `Message` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/run` | 200 / 400 / 401 / 404 | Run now; `require_developer`; allowed on foreign installs; response `Message` |
| `GET` | `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/logs` | 200 / 401 / 404 | Execution logs (last 50); response `AgentScheduleLogsPublic` |
| `GET` | `/api/v1/cli/account/agents/{agent_id}/status` | 200 / 401 / 404 | Status snapshot + configured pre-command; `?force_refresh=`; response `AccountAgentStatusResult` |
| `POST` | `/api/v1/cli/account/agents/{agent_id}/status/refresh-command` | 200 / 401 / 403 / 404 | Set `status_refresh_command`; `require_developer`; body `AccountStatusRefreshCommandBody`; response `AccountAgentStatusResult` |

**Phase 4 note:** No new routes were added. Agentic-teams team/node/connection CRUD
is reached entirely through `POST /api/v1/cli/account/api-proxy`. The agentic-teams
router prefix (`/agentic-teams`) is not on `EXCLUDED_PREFIXES`, so the full surface
(`POST agentic-teams`, `POST agentic-teams/{id}/nodes/`,
`POST agentic-teams/{id}/connections/`, `GET agentic-teams/{id}/chart`, etc.) is
proxyable verbatim.

**Knowledge search note:** No new models, no migration, no config knobs, no audit. `KnowledgeSearchBody` is reused from the per-agent route. The CLI-side MCP proxy wiring (`.mcp.json` entry that maps the `knowledge_query` tool to `POST /account/knowledge/search`) lives in the `cinna-cli` repo.

**Console-chat / file-upload note:** No new models, no migration, no config knobs, no security events. The only backend addition is the `POST /api/v1/cli/account/files/upload` route (one function in `backend/app/api/routes/cli.py`). Session-control routes are reached through the pre-existing api-proxy. The `cinna chat` client implementation lives in the `cinna-cli` repo.

Mint request body:
```json
{ "machine_name": "My MacBook", "machine_info": "optional string" }
```

Mint response (mirrors per-agent `exchange_setup_token`):
```json
{
  "token": "<child CLI JWT — shown once>",
  "id": "<child token UUID>",
  "agent_id": "<agent UUID>",
  "owner_id": "<user UUID>",
  "prefix": "first 12 chars",
  "expires_at": "ISO timestamp",
  "agent_name": "CRM Agent",
  "environment_id": "<UUID or null>",
  "template": "<env_name or null>",
  "frontend_url": "https://...",
  "knowledge_sources": []
}
```

## Pydantic Schemas

### `AccountAgentListItem`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | |
| `name` | str | |
| `description` | str \| None | |
| `ui_color_preset` | str \| None | |
| `owner_id` | UUID | |
| `user_workspace_id` | UUID \| None | |
| `bundle_uuid` | UUID \| None | |
| `is_publisher_install` | bool | |
| `is_foreign_install` | bool | Derived: `bundle_uuid is not None AND NOT is_publisher_install` |
| `can_build` | bool | Derived: `is_developer AND NOT is_foreign_install AND owner_id == user.id` |
| `has_active_environment` | bool | True if an `is_active=True` env row exists |

### `CLIAccountTokenPublic`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | |
| `name` | str | Machine name |
| `owner_id` | UUID | |
| `prefix` | str | First 12 chars of JWT (display only) |
| `is_revoked` | bool | |
| `last_used_at` | datetime \| None | |
| `machine_info` | str \| None | |
| `expires_at` | datetime | |
| `created_at` | datetime | |
| `child_count` | int | Count of active (non-revoked, non-expired) child tokens |

## Config Knobs (Phase 3)

Three new settings in `backend/app/core/config.py`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `ACCOUNT_API_PROXY_MAX_BODY_BYTES` | `1_048_576` (1 MiB) | Max request body size for escape hatch; exceeded → 413 |
| `ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES` | `8_388_608` (8 MiB) | Max inner response body; exceeded → 502 |
| `ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN` | `120` | Per-account-token sliding-window request limit; exceeded → 429 |

## Service Method Signatures

```python
# account_cli_service.py — AccountCLIService (Phase 3 additions)

@staticmethod
def create_account_setup_token(
    db: Session, user: User, request: Request
) -> CLISetupTokenCreated: ...

@staticmethod
async def exchange_account_setup_token(
    db: Session,
    token_str: str,
    machine_name: str,
    machine_info: str | None,
    request: Request,
) -> dict: ...

@staticmethod
def list_accessible_agents(db: Session, user: User) -> list[AccountAgentListItem]: ...

@staticmethod
async def mint_child_token(
    db: Session,
    user: User,
    account_token: CLIToken,
    agent_id: uuid.UUID,
    machine_name: str,
    machine_info: str | None,
    request: Request,
) -> dict: ...

@staticmethod
def list_account_tokens(db: Session, user: User) -> list[CLIAccountTokenPublic]: ...

@staticmethod
def revoke_account_token(
    db: Session, token_id: uuid.UUID, user: User
) -> int: ...  # returns count revoked

@staticmethod
async def revoke_child_token(
    db: Session,
    account_token: CLIToken,
    child_token_id: uuid.UUID,
    request: Request,
) -> None: ...
# Raises ValueError (→ 404) if child not found or not minted by account_token.
# No-op (no exception, no security event) if already revoked.

# agent_service.py — AgentService

@staticmethod
def is_foreign_install(agent: Agent) -> bool: ...

@staticmethod
def user_can_access(session: Session, user: User, agent: Agent) -> bool: ...

@staticmethod
def can_build(session: Session, user: User, agent: Agent) -> bool: ...

@staticmethod
def assert_can_build(session: Session, user: User, agent: Agent) -> None: ...
# raises CanBuildError(reason, message) where
#   reason = "not_accessible" | "not_developer" | "foreign_install"
```

## React Query Keys

| Key | Data | Invalidated by |
|-----|------|---------------|
| `["account-cli-tokens"]` | `CLIAccountTokensPublic` | `createAccountSetupToken` mutation success, `revokeAccountToken` mutation success |

Note: The `["account-cli-setup-token"]` key is not used in the current frontend
implementation; the generated setup token is kept in local component state
(`useState<CLISetupTokenCreated | null>`).
