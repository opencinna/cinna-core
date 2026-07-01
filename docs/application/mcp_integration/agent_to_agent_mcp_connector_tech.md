# Agent-to-Agent MCP Connector — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/mcp/mcp_connector.py` — `MCPConnector` (table model) + `MCPConnectorCreate` / `MCPConnectorUpdate` / `MCPConnectorPublic` — all now carry `is_agent_to_agent: bool = False`
- `backend/app/models/mcp/mcp_token.py` — `MCPToken` (table model) — carries `credential_id: UUID | None` (nullable FK to `credential.id`, `ON DELETE CASCADE`, added by migration `ab55mcpprovider01`)
- `backend/app/models/mcp/mcp_provider.py` — all request/response/projection schemas for the consumer-side routes, including `MCPProviderStatus` (carries `consumer_agent: MCPProviderTargetAgent | None` and `connector_mode: str | None` — the producer connector's single served mode), `MCPProviderTargetAgent` (shared shape for both producer and consumer agent projections: `id`, `name`, `ui_color_preset`), and `ConnectMcpProviderExternalRequest` (carries `user_workspace_id: UUID | None` so a manual external provider follows the user's active workspace)
- `backend/app/models/credentials/credential.py` — `CredentialType.MCP_PROVIDER` enum value; `mcp_mode_conversation` / `mcp_mode_building` / `mcp_consumer_agent_id` / `mcp_auth_mode` columns on `CredentialBase`; `MCPProviderData` (encrypted blob shape)
- `backend/app/models/__init__.py` — re-exports all new models

### Backend — Routes

- `backend/app/api/routes/mcp_connectors.py` — existing connector CRUD; `is_agent_to_agent` rides the existing create/update payloads unchanged
- `backend/app/api/routes/mcp_providers.py` — new consumer-side router; prefix `/api/v1/mcp-providers`
- `backend/app/api/main.py` — `mcp_providers` router registered

### Backend — Services

- `backend/app/services/mcp_providers/mcp_provider_service.py` — `MCPProviderService` (connect + status + discovery)
- `backend/app/services/mcp_providers/mcp_provider_oauth_service.py` — `MCPProviderOAuthService` (DCR + authorize + callback + refresh + probe)
- `backend/app/services/mcp_providers/egress_guard.py` — SSRF/egress guard (`validate_external_endpoint_url`, `is_host_blocked`, `assert_url_allowed`, `EgressBlockedError`)
- `backend/app/services/credentials/credentials_service.py` — `SENSITIVE_FIELDS["mcp_provider"]`, `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"]`, `collect_mcp_provider_manifest`, `_rewrite_mcp_endpoint_for_env`, `_refresh_expiring_mcp_provider` (pre-stream hook extension)
- `backend/app/services/environments/environment_lifecycle.py` — `_sync_mcp_servers_to_environment` (calls `collect_mcp_provider_manifest` + `adapter.set_mcp_servers`, triggered on env create/start/rebuild and on credential sync)
- `backend/app/services/environments/adapters/docker_adapter.py` — `set_mcp_servers(manifest)` (POST `/config/mcp-servers` to env-core)

### Backend — Configuration

- `backend/app/core/config.py`

| Setting | Default | Description |
|---------|---------|-------------|
| `MCP_SERVER_CONTAINER_URL` | `""` | Internal MCP origin used in the container URL rewrite for agent2agent connections (analogous to `AGENT_ENV_BACKEND_URL`). When unset the public `MCP_SERVER_BASE_URL` is used unchanged. |
| `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | Override for self-hosted deployments: disables the SSRF private-range block for external MCP targets. Leave `false` in production. |
| `MCP_PROVIDER_OAUTH_REDIRECT_URI` | `{FRONTEND_HOST}/mcp-providers/oauth/callback` | Redirect URI registered during DCR and used in the authorization-code exchange. |

### Backend — Migrations

| Revision | File | What it does |
|----------|------|-------------|
| `ab55mcpprovider01` | `ab55mcpprovider01_add_mcp_provider_connector_and_credential.py` | (1) `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'MCP_PROVIDER'` (autocommit block); (2) `mcp_connector.is_agent_to_agent BOOL NOT NULL DEFAULT false`; (3) `credential.mcp_mode_conversation BOOL NOT NULL DEFAULT true`, `credential.mcp_mode_building BOOL NOT NULL DEFAULT true`; (4) `mcp_token.credential_id UUID NULL FK → credential(id) ON DELETE CASCADE` + btree index. Downgrade drops the added columns/FK/index; leaves the enum value (PG cannot drop enum values safely). Down-revision: `2ca38822e945` (single head, verified). |
| `e5f972e7e32e` | `e5f972e7e32e_add_credential_mcp_consumer_agent_id.py` | Adds the consumer-agent pair column to `credential`: (1) `add_column('credential', UUID nullable)`; (2) `create_foreign_key('fk_credential_mcp_consumer_agent_id', 'credential', 'agent', ['mcp_consumer_agent_id'], ['id'], ondelete='SET NULL')`; (3) `create_index('ix_credential_mcp_consumer_agent_id', 'credential', ['mcp_consumer_agent_id'])`. Downgrade drops index, FK, column. No backfill — pre-existing agent2agent credentials remain `NULL`; an optional one-time backfill (separate step) can populate the column from existing `AgentCredentialLink` rows. Down-revision: `3c3c37a5e144`. |
| `b2d1f4c6a8e3` | `b2d1f4c6a8e3_add_credential_mcp_auth_mode.py` | Adds `credential.mcp_auth_mode VARCHAR NULL` — the cheap, non-secret auth-mode discriminator the UI tab classifier reads without decrypting (agent2agent → Automatic, external → My). Backfill: `UPDATE credential SET mcp_auth_mode='agent2agent' WHERE type='MCP_PROVIDER' AND id IN (SELECT credential_id FROM mcp_token WHERE credential_id IS NOT NULL)` — existing agent2agent rows are exactly those with a bound direct token; external rows are left `NULL` (classified "mine"). **Note: the `credentialtype` enum label is stored UPPERCASE in pg_enum, so the backfill matches `'MCP_PROVIDER'`.** Downgrade drops the column. Down-revision: `e5f972e7e32e` (single head). |

### Env-Core (inside container)

- `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /config/mcp-servers` route (`set_mcp_servers`) that accepts the manifest and writes `mcp/user_mcp.json` (0o600)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `set_mcp_servers(manifest)`, `get_user_mcp_servers_for_mode(mode, engine)`, `_read_user_mcp_manifest()`, `_translate_user_mcp_for_engine(entry, engine)` — reads `mcp/user_mcp.json` and translates entries for each SDK engine
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py` — `get_user_mcp_servers_for_mode` called during OpenCode config generation; entries merged into `opencode.json` `"mcp"` section under `cinna_mcp_<credential_id>` keys alongside plugin and bridge MCP servers
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` — same call at session start; entries merged into `options.mcp_servers` as HTTP/SSE server configs

### Frontend

- `frontend/src/components/Agents/McpConnectorsCard.tsx` — adds "Agent to Agent MCP Connector" sub-tab (4th tab) that lists and creates `is_agent_to_agent=true` connectors; `UserAllowlistPicker` for ACL; auto-enable `allow_token_access`
- `frontend/src/components/Credentials/ConnectMcpProviderDialog.tsx` — dialog with two paths: platform agent picker (uses `DiscoverableAgents` from `GET /mcp-providers/discoverable-agents`) and external server form (endpoint URL, transport, auth mode, token field, mode checkboxes); accepts a `defaultWorkspaceId` prop forwarded as `user_workspace_id` on the external connect (only when no `defaultConsumerAgentId`)
- `frontend/src/components/Credentials/McpProviderConnectionView.tsx` — `mcp_provider` credential detail panel; renders a left-to-right **MCP Client → MCP Server** schema (client = consumer `AgentBadge` + editable per-mode `Switch` toggles; server = producer `AgentBadge`/"External server" + status/auth-mode badges + read-only "Serves mode" indicator + endpoint copy) with compact Test/Reauthorize icon buttons in the server box corner; editable name/notes below
- `frontend/src/routes/_layout/credential/$credentialId.tsx` — `mcp_provider` branch: `McpProviderConnectionView` (owns name/notes + per-mode toggles) followed by `CredentialSharing` (role-gated). `CredentialTemplateSharing` omitted.
- `frontend/src/routes/_layout/credentials.tsx` — `mcp_provider` type added to the Automatic Credentials partition (alongside `agent_api`)
- `frontend/src/components/Credentials/AddCredential.tsx` — "Connect MCP Provider" entry point in the add-credential picker; passes `defaultWorkspaceId={workspaceFilter || undefined}` (from `useWorkspace`) so a new external provider lands in the page's active workspace
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` — per-agent "Connect MCP Provider" button (passes `consumer_agent_id`)
- `frontend/src/routes/mcp-providers/oauth/callback.tsx` — OAuth callback page for `oauth_dcr` flow; receives `code` + `state` from the authorization server, POSTs to `POST /mcp-providers/oauth/callback`, redirects to credential detail page on success
- `frontend/src/components/Credentials/credentialTypes.ts` — `mcp_provider` display meta (icon/label/badge); not offered in the manual type picker

### Tests

- `backend/tests/api/mcp_integration/test_a2a_connector_producer.py` — producer-side: `is_agent_to_agent` flag round-trip, discoverable-agents (owner / ACL user / inactive / non-a2a / own-agent exclusion / unauthenticated), partial-update preserves flag
- `backend/tests/api/mcp_integration/test_a2a_connector_consumer.py` — consumer-side: `connect_agent` full lifecycle (credential created, token bound, cascade revoke); non-ACL 403; missing/non-a2a connector 404; inactive blocked; non-owned consumer rejected; both-modes-off 400; token-binding cascade revoke; `connect_external` (fixed_token / none / oauth_dcr awaiting_auth); fixed_token requires token; invalid transport/auth_mode; SSRF (private IP / loopback / invalid scheme) blocked; both-modes-off; status non-owner 404; non-mcp_provider type 400
- `backend/tests/api/mcp_integration/test_a2a_connector_oauth_dcr.py` — egress guard (private IPv4 / loopback / link-local / bad scheme / missing host / valid public / allow-private override / DNS-resolve block); OAuth state single-use; state TTL expiry; PKCE S256; `_apply_token_response` field storage and missing-access-token error; `refresh_access_token` updates token + clears error; refresh failure stores `last_error`; `oauth_authorize` / `oauth_reauthorize` non-owner 404; `oauth_callback` valid-state code exchange; `oauth_callback` invalid-state 400

---

## Database Schema

### `mcp_connector` table (modified)

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `is_agent_to_agent` | `BOOL NOT NULL` | `false` | Marks a connector intended for agent-to-agent consumption. Drives the producer sub-tab grouping and the consumer picker discoverability. Does not change RS/token behavior. |

### `credential` table (modified)

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `mcp_mode_conversation` | `BOOL NOT NULL` | `true` | Inject this MCP provider into the conversation-mode SDK config. Only meaningful for `MCP_PROVIDER` rows; ignored for all other types. |
| `mcp_mode_building` | `BOOL NOT NULL` | `true` | Inject this MCP provider into the building-mode SDK config. Only meaningful for `MCP_PROVIDER` rows; ignored for all other types. |
| `mcp_consumer_agent_id` | `UUID NULL FK → agent.id ON DELETE SET NULL` | `NULL` | The consumer agent of an **agent2agent** `mcp_provider` pair. Set by `MCPProviderService.connect_to_agent` at connect time when a consumer agent is given. `NULL` for all other credential types and for external/manual `mcp_provider` credentials. Drives one-per-pair idempotency (Fix 5), auto-delete-on-unlink detection (Fix 4), and consumer-agent display in the credential detail UI (Fix 2). Index: `ix_credential_mcp_consumer_agent_id`. Added by migration `e5f972e7e32e`. |
| `mcp_auth_mode` | `VARCHAR NULL` | `NULL` | The auth mode of an `MCP_PROVIDER` credential, mirrored out of the encrypted blob into a cheap, non-secret column: `"agent2agent"` (auto-managed pair) or `"none"` / `"fixed_token"` / `"oauth_dcr"` (manual external). `NULL` for every other credential type. Read by `classify_credential_category` to assign the `/credentials` tab without decrypting: only `agent2agent` → "automatic"; external → "mine". Set at connect time by both `connect_to_agent` (`"agent2agent"`) and `connect_to_external` (`data.auth_mode`). Added by migration `b2d1f4c6a8e3`. |

`CredentialType` PostgreSQL native enum extended with `'MCP_PROVIDER'` (stored as uppercase member name, consistent with `'AGENT_API'`, `'API_TOKEN'`, etc.).

Both mode columns are surfaced through the read path that drives the detail-view toggles: `CredentialsService.get_credential_with_data()` explicitly copies `mcp_mode_conversation` / `mcp_mode_building` into its hand-built response dict. Omitting them (the prior bug) made `CredentialWithData` fall back to the model defaults (`True`/`True`) on every read, so a saved "disable" never reflected in the UI — the switch snapped back on after refetch.

`mcp_consumer_agent_id` is declared on `CredentialBase` (so it flows into `CredentialPublic` and create/update projections); the `sa_column` with the FK + `ondelete="SET NULL"` is on the `Credential` table model, mirroring the `user_workspace_id` SET-NULL pattern in the same file. The `_credential_to_public` helper in `credentials.py` and `read_agent_credentials` in `agents.py` both project `mcp_consumer_agent_id` on the public type.

### `mcp_token` table (modified)

| Column | Type | Description |
|--------|------|-------------|
| `credential_id` | `UUID NULL FK → credential(id) ON DELETE CASCADE` | Per-connection bound token (RD-2). Set by `connect_to_agent` immediately after credential creation. Nullable only transiently during the two-phase create (credential first, then token bind). `ON DELETE CASCADE` ensures deleting the consumer credential also deletes the token — revoking that consumer only. Index: `ix_mcp_token_credential_id`. |

### `credential.credential_data` shape for `mcp_provider` (`MCPProviderData`)

| Field | Secret | Description |
|-------|--------|-------------|
| `endpoint_url` | no | The MCP server URL the consumer's SDK connects to. For agent2agent: `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`. For external: the user-entered URL. |
| `transport` | no | `"streamable-http"` (default) or `"sse"`. |
| `auth_mode` | no | `"agent2agent"` / `"fixed_token"` / `"oauth_dcr"` / `"none"`. |
| `label` | no | Display name; seeds the SDK server key. |
| `target_agent_id` | no | (agent2agent only) producer agent UUID — UI display + back-reference. |
| `target_connector_id` | no | (agent2agent only) producer `mcp_connector` UUID. |
| `token` | **yes** | Bearer token sent as `Authorization: Bearer …`. For agent2agent: the producer's direct token; for `fixed_token`: the user-entered token; for `oauth_dcr`: the current (refreshed) access token. |
| `oauth_client_id` | no | (oauth_dcr) DCR-issued client id. |
| `oauth_client_secret` | **yes** | (oauth_dcr) DCR-issued client secret. Backend-only, never whitelisted. |
| `oauth_refresh_token` | **yes** | (oauth_dcr) Refresh token. Backend-only, never whitelisted. |
| `oauth_token_expires_at` | no | (oauth_dcr) Unix timestamp; drives pre-stream refresh. |
| `oauth_authorization_server` | no | (oauth_dcr) AS metadata base URL discovered from the resource. |
| `oauth_authorization_endpoint` | no | (oauth_dcr) Discovered authorization endpoint. |
| `oauth_token_endpoint` | no | (oauth_dcr) Discovered token endpoint. |
| `oauth_scope` | no | (oauth_dcr) Granted scopes. |
| `oauth_resource` | no | (oauth_dcr) RFC 8707 resource param (the endpoint URL). |
| `last_error` | no | Last error message (set on refresh failure; cleared on success). Drives the `error` status state. |

---

## Credential Pipeline

### `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"]`

`[]` — the credential is **never** written to `credentials.json`. The container has no general-purpose path to read the token.

### `SENSITIVE_FIELDS["mcp_provider"]`

`["token", "oauth_client_secret", "oauth_refresh_token"]` — these appear as `***REDACTED***` in any `README.md` or building-prompt credential summary. `endpoint_url`, `label`, `auth_mode`, `transport` are shown in clear.

### `collect_mcp_provider_manifest(session, agent_id, mode)` → `list[dict]`

Located in `CredentialsService`. For each `MCP_PROVIDER` credential linked to `agent_id` whose `mcp_mode_<mode>` is `True`:
1. Decrypt the credential blob.
2. Extract `endpoint_url`, `transport`, `token`, `auth_mode`.
3. For `auth_mode="agent2agent"`, apply `_rewrite_mcp_endpoint_for_env(url, auth_mode)` to swap the netloc to `MCP_SERVER_CONTAINER_URL` when set.
4. Build the manifest entry:
   ```python
   {
       "key": f"cinna_mcp_{credential_id}",
       "url": endpoint_url,  # (rewritten for agent2agent)
       "transport": transport,  # "streamable-http" | "sse"
       "headers": {"Authorization": f"Bearer {token}"} if token else {},
   }
   ```
5. Return the list; credentials with no `endpoint_url` are skipped with a warning.

The manifest is built separately for each mode (conversation / building), so a credential with `mcp_mode_building=False` is absent from the building manifest.

### `_rewrite_mcp_endpoint_for_env(endpoint_url, auth_mode)` → `str`

Only rewrites when `auth_mode == "agent2agent"` and `settings.MCP_SERVER_CONTAINER_URL` is non-empty. Swaps only the netloc (scheme + host + port) of the endpoint URL to the internal MCP origin, preserving the `/{connector_id}/mcp` path. Mirrors `_rewrite_agent_api_urls_for_env` exactly.

---

## API Routes (`/api/v1/mcp-providers`)

All routes use `SessionDep`, `CurrentUser`. Errors: `MCPProviderError` → `HTTPException` via `_handle_error`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/discoverable-agents` | Platform agents the current user may consume (`is_agent_to_agent=true`, active, user in ACL). Optional `consumer_agent_id` query param excludes the consumer's own agent. Returns `DiscoverableAgents`. |
| `POST` | `/connect/agent` | Connect to a platform agent2agent connector. Body: `ConnectMcpProviderAgentRequest`. ACL-checks; mints direct token; creates credential; optionally links to consumer agent. Returns `MCPProviderConnectionResponse`. |
| `POST` | `/connect/external` | Add an arbitrary external MCP server. Body: `ConnectMcpProviderExternalRequest`. For `fixed_token`/`none`: creates credential immediately (`status=connected`). For `oauth_dcr`: creates in `awaiting_auth`, starts DCR + authorization, returns `authorize_url`. Returns `MCPProviderConnectionResponse`. |
| `GET` | `/{credential_id}/status` | Derived connection status. Owner-only (404 on non-owner). Returns `MCPProviderStatus`. |
| `GET` | `/{credential_id}/oauth/authorize` | Begin (or re-run) the DCR + authorization flow. Returns `MCPProviderOAuthAuthorizeResponse` with the authorization URL. Owner-only. |
| `POST` | `/{credential_id}/oauth/reauthorize` | Re-run the authorization flow (revoked refresh token / scope change). Same as authorize but explicit verb. Owner-only. |
| `POST` | `/oauth/callback` | Authorization-code callback (public — CSRF `state` token provides authorization). Validates state, exchanges code (PKCE), stores tokens encrypted, triggers `event_credential_updated` sync, returns `MCPProviderOAuthCallbackResponse`. |
| `POST` | `/{credential_id}/test` | Best-effort connectivity probe: MCP `initialize` + `tools/list`. Owner-only. Returns `MCPProviderTestResult`. |

**Routes with changed behavior (no new routes, no request/response shape changes except additive fields):**

| Route | Behavior change |
|-------|----------------|
| `POST /connect/agent` | Now idempotent per (connector, consumer) pair; sets `mcp_consumer_agent_id` and `mcp_auth_mode="agent2agent"` on the new credential; returns existing credential on duplicate connect without creating a second token. |
| `POST /connect/external` | Now accepts `user_workspace_id` (used as the workspace when no consumer agent; ownership-validated) and sets `mcp_auth_mode=data.auth_mode` so the credential lands in **My Credentials**, in the user's active workspace. |
| `GET /{credential_id}/status` | Now includes `consumer_agent: MCPProviderTargetAgent \| None` (resolved from `mcp_consumer_agent_id`) and `connector_mode: str \| None` (the producer connector's served mode, agent2agent only). |
| `DELETE /api/v1/agents/{agent_id}/mcp-connectors/{connector_id}` | Now also deletes all agent2agent `mcp_provider` credentials built from the connector before deleting the connector. |
| `DELETE /api/v1/agents/{id}/credentials/{credential_id}` (unlink) | Now deletes the credential when the agent is the bound consumer of an agent2agent pair. |
| `POST /api/v1/agents/{id}/credentials` (link) | Now rejects re-homing an agent2agent credential to a different agent (`400`); auto-binds a floating agent2agent credential on first link. |

**Disconnect (manual)** = `DELETE /credentials/{id}` (existing blast-radius-gated route). Cascade-deletes the bound direct token (agent2agent only). No bespoke disconnect route. For agent2agent credentials the auto-cleanup paths above (connector-delete / consumer-unlink) are the primary disconnect triggers; manual `DELETE /credentials/{id}` also works and still goes through the blast-radius gate.

---

## Service Layer

### `MCPProviderService` (`backend/app/services/mcp_providers/mcp_provider_service.py`)

- `build_endpoint_url(connector_id)` → `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`
- `list_discoverable_agents(session, user, consumer_agent_id)` → `list[DiscoverableAgent]` — queries `mcp_connector` where `is_agent_to_agent=True` and `is_active=True`, filters by `MCPConnectorService.check_user_access`, excludes the consumer's own agent, resolves agent names.
- `connect_to_agent(session, user, data, is_superuser)` → `MCPProviderConnectionResponse` — validates modes; resolves connector (requires `is_agent_to_agent` and `is_active`); ACL-checks caller; validates consumer agent ownership up front; **pre-checks for an existing pair** (idempotency — if an agent2agent credential for this exact (connector, consumer agent) pair already exists, returns it immediately without minting a new token or creating a new credential; see Fix 5); mints direct token via `MCPDirectTokenService.create_token`; creates `mcp_provider` credential via `CredentialsService.create_credential` (workspace consumer-first), **sets `mcp_consumer_agent_id = consumer_agent.id`** when consumer is given, and **sets `mcp_auth_mode = "agent2agent"`** (→ Automatic Credentials tab); binds token to credential (`mcp_token.credential_id = credential.id`); optionally links to consumer agent. Orphan-cleanup: on credential creation failure, the minted token is deleted (best-effort) to avoid an unrevokable grant.
- `connect_to_external(session, user, data, is_superuser)` → `MCPProviderConnectionResponse` — validates modes, transport, auth_mode; SSRF-validates endpoint URL via `validate_external_endpoint_url`; resolves the credential workspace consumer-first: a consumer agent's workspace wins, else `data.user_workspace_id` (the page's active workspace, ownership-validated against `UserWorkspace.user_id` — mirrors `POST /credentials`), else NULL/default; creates credential with **`mcp_auth_mode = data.auth_mode`** (→ My Credentials tab); for `oauth_dcr`, calls `MCPProviderOAuthService.begin_authorization` and returns the `authorize_url`.
- `get_status(session, credential_id, user, is_superuser)` → `MCPProviderStatus` — decrypts credential, calls `_to_status`, **resolves `consumer_agent`** from `credential.mcp_consumer_agent_id` and **`connector_mode`** from the bound connector (`target_connector_id` in the blob → `session.get(MCPConnector, ...).mode`), projecting both into `MCPProviderStatus`. `consumer_agent` is `None` when the column is `NULL` or the agent was deleted; `connector_mode` is `None` for external providers or a deleted connector. Owner-only.
- `get_owned_credential(session, credential_id, user, is_superuser)` → `Credential` — shared by OAuth and test routes; 404 on non-owner / wrong type.
- `_derive_lifecycle_state(auth_mode, data)` → `str` — pure function; derives `connected` / `awaiting_auth` / `expired` / `error` from the decrypted blob without DB access.
- `_validate_modes(conversation, building)` — raises `MCPProviderError(400)` if both are `False`.

**Exception hierarchy:**
- `MCPProviderError(message, status_code)` — 400/403/404

### `CredentialsService` — agent2agent hooks (`backend/app/services/credentials/credentials_service.py`)

**New helpers:**

- `_is_agent2agent_mcp_provider(credential)` → `bool` — decrypts the credential blob and returns `True` when `type == MCP_PROVIDER` and `auth_mode == "agent2agent"`. Single place for the agent2agent distinction; called by link, unlink, and delete hooks below. Decryption is skipped for non-`MCP_PROVIDER` types via a cheap type-check first.
- `classify_credential_category(*, is_owned, credential_type, share_source, mcp_auth_mode=None)` → `"mine" | "automatic" | "bundle"` — the single source-of-truth UI tab classifier. For owned `MCP_PROVIDER` rows it now consults `mcp_auth_mode`: `"agent2agent"` → `"automatic"`, anything else (external `none`/`fixed_token`/`oauth_dcr`, or `NULL`) → `"mine"`. `AGENT_API` stays unconditionally `"automatic"`. Callers (`read_credentials` projection in `credentials.py`) pass `mcp_auth_mode=c.mcp_auth_mode` — no decryption needed (the column is the discriminator). The shared-credentials caller passes `is_owned=False` (never "automatic"), so `mcp_auth_mode` is irrelevant there.
- `_delete_credential_internal(session, credential, *, reason)` — gate-bypassing delete: removes the credential row (DB cascade revokes the bound `mcp_token` and `AgentCredentialLink` rows), collects the affected agent IDs before deletion, dispatches `event_credential_deleted` for consumer env-sync (mirrors `delete_credential` L1866-1879). Used by auto-cleanup paths where the normal blast-radius gate must not run.

**Modified methods:**

- `link_credential_to_agent` (L2151-2205) — agent2agent guard added: if `_is_agent2agent_mcp_provider(credential)` and `mcp_consumer_agent_id is not None` and `mcp_consumer_agent_id != agent_id` → raises `ValueError` (route maps to `400`). If `mcp_consumer_agent_id is None` (floating connection, i.e. created without a consumer then later linked manually) → **binds** by setting `mcp_consumer_agent_id = agent_id` on first link. External/manual providers (`auth_mode != "agent2agent"`) pass through unchanged.
- `unlink_credential_from_agent` (L2207-2251) — auto-delete hook added: after the standard unlink, if `_is_agent2agent_mcp_provider(credential)` and `credential.mcp_consumer_agent_id == agent_id` → calls `_delete_credential_internal` (credential deleted, token cascaded, env-sync dispatched). If the unlinked agent is not the bound consumer, or the credential is external/manual, the credential survives (plain unlink only, unchanged).

### `MCPConnectorService` — connector-delete cleanup (`backend/app/services/mcp/mcp_connector_service.py`)

**Modified method:**

- `delete_connector` (L96-118) — before `db_session.delete(connector)`, resolves all agent2agent `mcp_provider` credentials built from this connector via `select(MCPToken).where(MCPToken.connector_id == connector_id, MCPToken.credential_id.is_not(None))`, collects distinct `credential_id`s, loads each `Credential`, gates on `_is_agent2agent_mcp_provider` (defensive), then calls `CredentialsService._delete_credential_internal` for each (bypass blast-radius gate, fire consumer env-sync). Then proceeds with the existing connector delete + `mcp_registry.remove` + commit. Order is important: token resolution must happen before `db_session.delete(connector)` would cascade-destroy the bound tokens.

### `MCPProviderOAuthService` (`backend/app/services/mcp_providers/mcp_provider_oauth_service.py`)

Backend-side OAuth 2.1 / DCR client. `client_secret` and `refresh_token` never leave the backend.

- `discover_authorization_server(endpoint_url)` → AS metadata dict — fetches `/.well-known/oauth-protected-resource` on the endpoint origin, follows `authorization_servers[0]` to the AS base, fetches `/.well-known/oauth-authorization-server`. Falls back to the endpoint origin as the AS base if the protected-resource document is absent. All fetches go through `assert_url_allowed` (RD-6).
- `register_client(as_metadata, redirect_uri, resource)` → `{client_id, client_secret}` — DCR (RFC 7591) `POST {registration_endpoint}`. Surfaces user-actionable error when AS does not support DCR.
- `begin_authorization(session, credential, user_id)` → `authorize_url` — discovers AS, performs DCR (idempotent: reuses existing `oauth_client_id` if present), persists client creds + AS endpoints, builds PKCE S256 pair, stores CSRF state (`_put_state`), returns authorization URL with `client_id` / `redirect_uri` / `state` / `code_challenge` / `code_challenge_method` / `resource` params.
- `handle_callback(session, code, state)` → `Credential` — validates + pops CSRF state (`_take_state`), exchanges authorization code (PKCE verifier) for tokens via `_token_request`, calls `_apply_token_response` (stores `token` / `oauth_refresh_token` / `oauth_token_expires_at` / `oauth_scope`), persists encrypted, returns updated credential.
- `refresh_access_token(session, credential)` → `Credential` — refresh grant via `_token_request`; on failure records `last_error` on the credential and re-raises; caller continues with stale token (graceful).
- `probe(session, credential)` → `dict` — opens MCP `initialize` + `tools/list` against the endpoint; returns `{ ok, tools, error }`. Goes through `assert_url_allowed`. On `httpx.HTTPError` the `error` detail is built robustly (offline host / DNS failure / refused connection): `str(e)`, then the underlying `e.__cause__` OS message in parens when present, falling back to the exception class name — so the Test toast never shows a bare "Connection failed:" with no detail.

**CSRF state store:** in-memory `_oauth_states` dict, keyed by random nonce, 10-minute TTL. `_put_state` / `_take_state` (single-use). Mirrors the Google OAuth `_oauth_states` pattern; moves to Redis in a multi-worker deployment.

**`MCPProviderOAuthError(message, status_code)`** — 400/404

### Egress Guard (`backend/app/services/mcp_providers/egress_guard.py`)

Single chokepoint for all backend-initiated calls to external MCP URLs.

- `validate_external_endpoint_url(url)` → normalized URL — static check: scheme must be `http` or `https`; host must be present; literal IP private ranges rejected. Honors `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS`.
- `is_host_blocked(host)` → bool — DNS-resolving check: resolves hostname, blocks if any resolved IP is private/loopback/link-local/multicast/reserved/unspecified. Network-time guard.
- `assert_url_allowed(url)` → normalized URL — combines both checks; used by `MCPProviderOAuthService` on every outbound request.
- `EgressBlockedError` — raised on any violation.

---

## Env-Core: MCP Provider Route and Injection

### `POST /config/mcp-servers` (env-core `routes.py`)

Receives `McpServerManifest` (per-mode entries) from the host-side adapter. Calls `agent_env_service.set_mcp_servers(manifest)` which:
1. Writes `mcp/user_mcp.json` (0o600 — entries may carry a bearer token).
2. Returns `{"conversation": N, "building": N}` counts.

This is the live-push path (RD-5, mirrors `POST /config/plugins`). The route requires a running env-core process; shipping it requires an env rebuild after template changes (same requirement as the plugins route).

### `agent_env_service.set_mcp_servers` / `get_user_mcp_servers_for_mode`

`set_mcp_servers(manifest)` — writes the per-mode MCP manifest to `mcp/user_mcp.json` with `0o600` permissions.

`get_user_mcp_servers_for_mode(mode, engine)` → `dict` — reads `mcp/user_mcp.json`, filters to `mode`, translates each entry via `_translate_user_mcp_for_engine(entry, engine)`:
- **OpenCode**: `{ "type": "remote", "url": …, "headers": { "Authorization": "Bearer …" }, "enabled": true }` (entries with no `url` are skipped).
- **Claude Code**: `{ "type": "http", "url": …, "headers": { "Authorization": "Bearer …" } }` or `{ "type": "sse", "url": … }` for `transport=sse`. Returns in the format `claude_agent_sdk` `MCPServer` expects.

### SDK Merge Points

**OpenCode** (`opencode_sdk_adapter.py`): after building plugin MCP servers, `get_user_mcp_servers_for_mode(self._mode, "opencode")` is called; entries are merged into the `"mcp"` dict keyed `cinna_mcp_<credential_id>`. Collision prevention: the `cinna_mcp_` prefix differs from the `plugin_` prefix used by plugin-declared servers and the bridge-server names used by the existing MCP bridge servers.

**Claude Code** (`claude_code_sdk_adapter.py`): at session start, `get_user_mcp_servers_for_mode(mode, "claude_code")` is called; entries are merged into `options.mcp_servers` (alongside `knowledge` and `agent_task` servers). Each `cinna_mcp_<credential_id>` key is logged at DEBUG level.

---

## Frontend Components

### `McpConnectorsCard.tsx` (modified)

A fourth tab **"Agent to Agent MCP Connector"** has been added alongside the existing connector types. The create dialog:
- Name field.
- Mode selector (conversation / building).
- `UserAllowlistPicker` for `allowed_user_ids` (which users may consume).
- Forced `allow_token_access=true` (agent2agent requires a direct token; the consumer connect helper mints it).
- `is_agent_to_agent=true` set on create payload.

The list shows only `is_agent_to_agent=true` connectors (filtered client-side from the shared list endpoint). Non-a2a connectors continue to show in the other tabs unchanged.

**Edit dialog (agent2agent):** The shared edit dialog is gated on `editingConnector.is_agent_to_agent`. When `true`, only the create-form fields are shown (name, mode, `UserAllowlistPicker`); the "Allow token access" Switch + `McpDirectTokensManager` block and the "MCP Server URL" block are hidden (`!editingConnector.is_agent_to_agent` guard). The save payload does not send `allow_token_access` as `false` for agent2agent connectors — `allow_token_access` remains `true` server-side regardless. For external/direct connectors the edit dialog is unchanged.

### `ConnectMcpProviderDialog.tsx`

Two-mode dialog:
- **Platform agent** (`flow === "platform"`): feeds `GET /mcp-providers/discoverable-agents?consumer_agent_id=…`; displays `DiscoverableAgent` rows (agent Bot badge, connector name, mode); on select → `POST /connect/agent`. **No URL field** — the endpoint is derived server-side automatically. A code comment in the component marks this intentionally (so a future refactor cannot reintroduce the field on this path).
- **External server** (`flow === "external"`): form with endpoint URL (shown only here), transport select (`streamable-http` / `sse`), auth mode select (`none` / `fixed_token` / `oauth_dcr`), conditional token field for `fixed_token`, mode checkboxes. On submit → `POST /connect/external`, sending `user_workspace_id` from the `defaultWorkspaceId` prop (only when there is no consumer agent — so a manual external provider lands in the user's active workspace). For `oauth_dcr`, the response `authorize_url` triggers a browser redirect or popup; on OAuth callback return, status becomes `connected`.

The `defaultWorkspaceId` prop is supplied by `AddCredential.tsx` from `useWorkspace().workspaceFilter` (the `/credentials` page's active-workspace filter); per-agent callers (`AgentCredentialsTab.tsx`) pass a `defaultConsumerAgentId` instead, so the agent's workspace wins and `defaultWorkspaceId` is ignored.

### `McpProviderConnectionView.tsx`

Renders the connection as a **left-to-right schema** so the user reads where the client is, where the server is, and which modes are involved. Driven by the `MCPProviderStatus` response from `GET /{id}/status` (the `["mcp-provider-status", credentialId]` query) plus the per-mode columns on the credential.

- **Left — MCP Client**: the consumer agent (`status.consumer_agent`, from `mcp_consumer_agent_id`) as the shared `AgentBadge`; falls back to a neutral dashed "Any linked agent" pill when null (external/unbound). Owns the editable **per-mode `Switch` toggles** (`mcp_mode_conversation` / `mcp_mode_building`), each firing its own `PATCH /credentials/{id}`. Inert warning when both are off.
- **Connector**: a centered arrow node pointing client → server (rotates to point down when the boxes stack on mobile).
- **Right — MCP Server**: the producer agent (`status.target_agent`) as an `AgentBadge`, or an "External server" badge; status badge (green `connected`, amber `awaiting_auth`/`expired`, red `error`); auth-mode badge **shown only for external servers** (redundant for agent2agent). A read-only **"Serves mode"** indicator reflects the producer connector's single served mode (`status.connector_mode` — Conversation lit, Building muted/struck-through, or vice versa); the block is **omitted entirely for external servers** (no notion of modes). Copyable endpoint URL (external). Compact **Test** (`POST /{id}/test`) and **Reauthorize** (`oauth_dcr` only, `POST /{id}/oauth/reauthorize`) **icon buttons** sit in the box's top-right corner — each swaps its icon for a `Loader2` spinner while pending (a plain `Button`, not `LoadingButton`, to avoid rendering both icon and spinner at once on an icon-only button).
- **Below the schema**: editable name/notes form (metadata-only `PATCH /credentials/{id}` with `{name, notes}`); `last_error` text for the `error` state.

### `$credentialId.tsx` — `mcp_provider` branch

Renders `McpProviderConnectionView` (the two-column card owns name/notes and the per-mode toggles) followed by `CredentialSharing` (role-gated). `CredentialTemplateSharing` is NOT rendered (a connection has no user-fillable private fields).

### `credentials.tsx` — tab partition (server `category`)

The page partitions cards by the **server-computed `category`** field (`c.category`, defaulting to `"mine"`) — it never re-derives provenance or automatic-ness client-side. So an `mcp_provider` lands in the right tab purely from the backend `classify_credential_category` result: agent2agent → **Automatic Credentials** (alongside `agent_api`), external → **My Credentials**. No frontend type-list edit is needed; the `mcp_auth_mode` discriminator drives it entirely on the backend.

### `mcp-providers/oauth/callback.tsx`

Public route (no auth dependency) at `/mcp-providers/oauth/callback`. Reads `code` + `state` from URL params, POSTs to `POST /api/v1/mcp-providers/oauth/callback` (the CSRF `state` is the authorization), then redirects to the credential detail page (or posts back to the opener popup) on success, or shows an error banner. The callback mutation is guarded by a `useRef` so it fires **exactly once per mount** — without it, React 18 StrictMode's dev double-invoke of `useEffect` fired a second POST whose now-consumed single-use `state` returned `400`, surfacing a spurious "Authorization failed" even though the first POST had already exchanged the code successfully. The backend keeps `state` single-use (anti-replay); the guard only stops the client self-replaying.

### React Query Keys

- `["mcp-providers", "discoverable-agents", consumerAgentId]` — platform agent picker
- `["mcp-provider-status", credentialId]` — connection status badge (live on detail page)
- `["credentials"]` — invalidated after connect/disconnect mutations; auto-cleanup on connector-delete / consumer-unlink means the removed agent2agent credential disappears from the Automatic Credentials list without any additional invalidation (the existing `["credentials"]` invalidation already covers it).

---

## agent_api vs agent2agent MCP: Divergence on Consumer Binding

Both features use the producer/consumer + connection-is-a-credential + Automatic Credentials architecture. They now differ on how the consumer side is recorded and what happens on disconnect:

| Aspect | `agent_api` | `agent2agent mcp_provider` |
|--------|------------|---------------------------|
| Consumer recorded as | `AgentCredentialLink` only (no consumer column) | `Credential.mcp_consumer_agent_id` column + `AgentCredentialLink` |
| Connect same pair twice | Creates a second credential | Idempotent — returns existing |
| Link to different consumer | Allowed (freely relinkable) | Rejected with `400` |
| Unlink from bound consumer | Credential survives (plain unlink) | Credential auto-deleted |
| Producer deleted | Credentials survive (point at dead endpoint) | Agent2agent credentials auto-deleted |

The divergence is intentional: an MCP pair is semantically stricter than an agent_api connection (exactly one consumer, lifetime tied to the connection). The `agent_api` twin retains looser many-to-many semantics.

---

*Last updated: 2026-06-24*
