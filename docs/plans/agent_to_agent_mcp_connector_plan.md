# Agent-to-Agent MCP Connector Network — Implementation Plan

## Overview

Let one platform agent connect to another platform agent **as an MCP connector**, building a peer-to-peer MCP network using the same MCP protocol we already expose to external clients (Claude Desktop, Cursor). The connection is delivered to the consuming agent's SDK as a **first-class MCP provider injected into the SDK config** (Claude Code `options.mcp_servers` / OpenCode `opencode.json` `mcp` block) — **not** as a credential file. Generalizing the same machinery, a user can also attach **arbitrary external MCP servers** (fixed-token or OAuth/DCR), so the platform performs DCR + token refresh on the user's behalf.

This mirrors the `agent_api` producer/consumer architecture: a **connection is a credential**, created by a one-click "Connect" helper, that rides the existing credential sync / whitelist / redaction / sharing pipeline. The difference from `agent_api` is the consumer-side materialization target: instead of writing `credentials.json`, the new credential type is translated into an MCP server entry in the SDK runtime config, per selected mode (conversation / building).

Core capabilities:
- **Producer side**: a 4th sub-tab "Agent to Agent MCP Connector" in the Integrations → MCP Connectors card that exposes an agent over MCP for other *agents* (reuses the existing `mcp_connector` + direct-token + user-ID ACL infrastructure).
- **Consumer side**: a new `MCP_PROVIDER` credential type + a "Connect MCP Provider" button (mirror of "Connect Agent API"), with two flows: (a) connect to another platform agent's agent2agent connector, (b) add an arbitrary external MCP server.
- **Per-mode SDK injection**: the credential becomes an MCP server inside the SDK for the selected modes; reuses the plugin-declared-MCP-server registration path.
- **OAuth/DCR**: DCR + authorization-code + refresh for arbitrary MCP servers, modeled on the existing `oauth_credentials` refresh machinery (pre-stream refresh).
- **Sharing**: full `CredentialShare` reuse (user / publisher / template); sharing an agent2agent connection = sharing an agent's MCP surface.

### High-Level Flow

```
PRODUCER SIDE                                       CONSUMER SIDE
┌────────────────────────────────┐                 ┌──────────────────────────────────────┐
│ Agent A (owner)                │                 │ Agent B (owner)                       │
│ Integrations → MCP Connectors  │                 │ Credentials tab                       │
│   → "Agent to Agent MCP        │                 │   → "Connect MCP Provider"            │
│      Connector" sub-tab        │                 │       ├─ (a) pick platform agent A    │
│   creates mcp_connector        │                 │       └─ (b) add external MCP URL     │
│   + a2a-scoped direct token    │                 │   creates MCP_PROVIDER credential     │
│   + user-ID ACL (share w/users)│                 │   (endpoint, auth_mode, modes, token) │
└──────────────┬─────────────────┘                 └───────────────────┬──────────────────┘
               │ exposes /mcp/{connector_id}/mcp                        │ credential sync (NOT a file)
               ▼                                                        ▼
   ┌──────────────────────── Backend ───────────────────────┐   ┌──────────────────────────────┐
   │ existing per-connector MCPServer (RS)                   │   │ env-core builds SDK MCP config│
   │ token verifier accepts a2a direct token                │◄──│   per mode (conv / building)  │
   └─────────────────────────────────────────────────────────┘   │   opencode.json "mcp" / CC    │
                                                                  │   options.mcp_servers         │
                                                                  └───────────────────────────────┘
            OAuth/DCR external servers: backend MCPProviderOAuthService runs DCR + code + refresh,
            stores tokens encrypted on the credential, pre-stream refresh injects a fresh Bearer.
```

---

## Architecture Overview

### Producer side — reuse, don't rebuild

The producer half is **almost entirely existing infrastructure**. An agent is already exposed as a remote MCP server via `mcp_connector` (`backend/app/mcp/server.py`, `MCPServerRegistry`), already supports a user-ID ACL (`allowed_user_ids`, migration `cdfb21cadb62`), and already supports connector-scoped direct tokens (`token_type="direct"`, `MCPDirectTokenService`). The agent already exposes the canonical `send_message` tool.

The only thing missing on the producer side is a **purpose-built UI affordance + a thin convenience flow** that:
1. Creates an `mcp_connector` flagged as an *agent-to-agent* connector (so it shows in the right sub-tab and is discoverable by the consumer "Connect" picker).
2. Mints a connector-scoped direct token (the machine credential another agent uses) rather than forcing the OAuth flow.
3. Manages the user-ID ACL (who may consume it) via the shared `UserAllowlistPicker`.

**Decision (see Open Questions OQ-1):** extend the existing `mcp_connector` model with an `is_agent_to_agent: bool` flag and reuse everything, rather than introducing a parallel connector model. The connector already has `mode`, `allowed_user_ids`, `allow_token_access`, direct tokens, and a working RS endpoint.

### Consumer side — new credential type, new materialization target

The consumer half is the genuinely new surface:

- A new `CredentialType.MCP_PROVIDER` whose `credential_data` is `{ endpoint_url, auth_mode, transport, label, target_agent_id, target_connector_id, token, oauth_* }`.
- It rides the credential sync / whitelist / redaction / sharing pipeline like `agent_api`, **but is excluded from `credentials.json`**. Instead, on env sync it is collected into a per-mode **MCP server manifest** and injected into the SDK config.
- A "Connect MCP Provider" helper (two flows) creates the credential, exactly mirroring "Connect Agent API".
- For OAuth/DCR external servers, a backend OAuth client (`MCPProviderOAuthService`) performs DCR against the target's AS, runs the authorization-code flow (browser consent, same shape as Google `oauth_credentials`), and stores `client_id` / `client_secret` / `access_token` / `refresh_token` / `expires_at` encrypted on the credential. A pre-stream refresh keeps the access token fresh.

### The two SDK injection points (both already exist)

| Engine | Existing MCP injection point | New use |
|--------|------------------------------|---------|
| **OpenCode** | `opencode.json` `"mcp"` block (built host-side in `environment_lifecycle._generate_opencode_config_files`, line ~2184), plus the plugin path `get_opencode_plugin_artifacts()` that merges plugin-declared MCP servers under `plugin_<...>` keys | Add credential-derived remote MCP servers (`type: "remote"`, `url`, `headers: {Authorization: Bearer …}`, `enabled: true`) under `cinna_mcp_<credential_id>` keys |
| **Claude Code** | `claude_code_sdk_adapter.py` builds `options.mcp_servers` (currently `knowledge`, `agent_task`) — supports HTTP/SSE remote MCP server configs | Merge credential-derived remote MCP servers into `options.mcp_servers` filtered by mode |

Both are **per-mode** already (OpenCode generates one config per mode; Claude Code reads `mode` at session start), so per-mode applicability (conversation/building) is naturally enforced at the injection site.

### Data flow (consumer)

```
MCP_PROVIDER credential (DB, encrypted)
   │  credential link to agent B (existing AgentCredentialLink)
   ▼
CredentialsService.prepare_credentials_for_environment(...)
   │  ├─ MCP_PROVIDER excluded from credentials.json (whitelist returns [] for the file path)
   │  └─ MCP_PROVIDER collected into a per-mode MCP manifest (new path)
   ▼
EnvironmentLifecycleManager.sync  →  adapter.set_user_mcp_servers(manifest)   (new env-core route)
   │     OR (simpler) env file/opencode.json regeneration includes the manifest
   ▼
env-core writes per-mode runtime MCP config
   ├─ OpenCode: merge into opencode.json "mcp"
   └─ Claude Code: expose for sdk_manager → options.mcp_servers
```

### Integration with existing systems

- **MCP integration** (`docs/application/mcp_integration/*`): producer reuses `mcp_connector`, `MCPDirectTokenService`, `MCPTokenVerifier`, `MCPServerRegistry`.
- **Agent credentials** (`docs/agents/agent_credentials/*`): consumer rides the credential pipeline; new whitelist/redaction/sharing entries.
- **OAuth credentials** (`docs/agents/agent_credentials/oauth_credentials.md`): DCR/refresh modeled on the pre-stream refresh + `event_credential_updated` machinery.
- **Multi-SDK / env-core** (`docs/agents/agent_environment_core/multi_sdk_tech.md`): per-mode SDK MCP injection points.
- **Agent plugins** (`docs/agents/agent_plugins/agent_plugins_tech.md`): the plugin-declared-MCP-server merge is the template for credential-declared-MCP-server merge.
- **`agent_api`** (`docs/agents/agent_api/*`): the entire producer/consumer + "Connect X" + connection-is-a-credential + sharing pattern.

---

## Data Models

### Modified: `mcp_connector` (producer flag)

Add one column to the existing table (`backend/app/models/mcp/mcp_connector.py`):

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `is_agent_to_agent` | `BOOL NOT NULL` | `false` | Marks a connector intended for agent-to-agent consumption. Drives the producer sub-tab grouping and the consumer "Connect" picker discoverability. Does not change RS/token behaviour. |

- `MCPConnectorCreate` / `MCPConnectorUpdate` / `MCPConnectorPublic` gain `is_agent_to_agent`.
- No new producer model is required. Agent-to-agent connectors typically set `allow_token_access=true` (so the convenience flow can mint a direct token) but that remains the existing flag.

### New enum value: `CredentialType.MCP_PROVIDER`

Add `MCP_PROVIDER = "mcp_provider"` to `CredentialType` (`backend/app/models/credentials/credential.py`). Native Postgres enum → needs a non-transactional `ALTER TYPE … ADD VALUE` migration (mirrors `aa44agentapi04`).

`credential_data` (encrypted) shape:

| Field | Meaning |
|-------|---------|
| `endpoint_url` | The MCP server URL the consumer's SDK connects to. For agent2agent: `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`. For external: the user-entered URL. |
| `transport` | `"streamable-http"` (default) or `"sse"`. |
| `auth_mode` | `"agent2agent"` \| `"fixed_token"` \| `"oauth_dcr"` \| `"none"`. |
| `label` | Display name / SDK server key seed. |
| `target_agent_id` | (agent2agent only) producer agent UUID — for UI display + back-reference. |
| `target_connector_id` | (agent2agent only) producer `mcp_connector` UUID. |
| `token` | The bearer token sent as `Authorization: Bearer …`. For agent2agent this is the producer's direct token; for `fixed_token` it's the user-entered token; for `oauth_dcr` it's the *current access token* (refreshed). |
| `oauth_client_id` | (oauth_dcr) DCR-issued client id. |
| `oauth_client_secret` | (oauth_dcr) DCR-issued client secret (backend-only, never whitelisted). |
| `oauth_refresh_token` | (oauth_dcr) backend-only, never whitelisted. |
| `oauth_token_expires_at` | (oauth_dcr) unix ts; drives pre-stream refresh. |
| `oauth_authorization_server` | (oauth_dcr) AS metadata base URL discovered from the resource. |
| `oauth_scope` | (oauth_dcr) granted scopes. |
| `oauth_resource` | (oauth_dcr) RFC 8707 resource param (the endpoint URL). |

The credential also carries the **per-mode applicability** via two non-secret columns (so the env-sync collector can filter without decrypting more than needed). Add to the `credential` table:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `mcp_mode_conversation` | `BOOL NOT NULL` | `true` | Inject this MCP provider into conversation-mode SDK config. |
| `mcp_mode_building` | `BOOL NOT NULL` | `true` | Inject into building-mode SDK config. |

> These two booleans live on the `credential` table (not inside `credential_data`) so the per-mode manifest collector can read them cheaply and so they appear plainly in the credential detail UI. They are only meaningful for `MCP_PROVIDER` rows; ignored for all other types.

**Lifecycle states** (derived, not stored): `connected` (token present / OAuth valid), `awaiting_auth` (oauth_dcr, no token yet), `expired` (oauth token past expiry, refresh pending), `error` (last refresh failed). Surfaced on the credential detail page like the OAuth status badge.

### Per-connection bound token (no new token table)

Agent2agent connections reuse `mcp_token` with `token_type="direct"`. Each connection mints a **distinct** direct token bound to its consumer `MCP_PROVIDER` credential via a nullable `mcp_token.credential_id` FK (`ON DELETE CASCADE`), mirroring `agent_api_token.credential_id`. Disconnect = delete the consumer credential = revoke **that consumer only** (RD-2), without affecting other consumers of the same producer connector.

### Migration summary

A single migration off the current `head = 2ca38822e945` (single head verified via `alembic heads`; trim autogen drift):
1. `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'mcp_provider'` (non-transactional, separate from the rest if needed — enum value adds cannot run in the same transaction as the table ops on some PG versions; follow the `aa44agentapi04` precedent and split if required).
2. `ALTER TABLE mcp_connector ADD COLUMN is_agent_to_agent BOOL NOT NULL DEFAULT false`.
3. `ALTER TABLE credential ADD COLUMN mcp_mode_conversation BOOL NOT NULL DEFAULT true`, `ADD COLUMN mcp_mode_building BOOL NOT NULL DEFAULT true`.
4. `ALTER TABLE mcp_token ADD COLUMN credential_id UUID NULL REFERENCES credential(id) ON DELETE CASCADE` + btree index (per-connection bound token — RD-2).

Downgrade: drop columns; enum value removal is a no-op (PG can't drop enum values cleanly — leave it, matching prior enum migrations).

---

## Security Architecture

- **Encryption**: `credential_data` (including `token`, `oauth_*`) encrypted at rest via existing Fernet credential encryption (`backend/app/core/security.py`). No new crypto.
- **Whitelist (`AGENT_ENV_ALLOWED_FIELDS["mcp_provider"]`)**: the MCP provider is **not** written to `credentials.json`. Two options:
  - Preferred: set `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] = []` so the credential never reaches `credentials.json`, and add a **separate collection path** that reads the (decrypted) `token`/`endpoint_url` directly for the MCP manifest. The manifest is written to a runtime config the agent's *scripts* cannot read as easily as `credentials.json` (it lives in the SDK config dir, `0o600`).
  - This preserves the principle "secrets only where they must be." The token must reach the SDK MCP transport, so it lands in the per-mode SDK config (already `0o600`, same as the embedded LLM API key in `opencode.json`).
- **Redaction (`SENSITIVE_FIELDS["mcp_provider"]`)**: `token`, `oauth_client_secret`, `oauth_refresh_token` are redacted as `***REDACTED***` in any README/prompt summary. `endpoint_url`, `label`, `auth_mode`, `transport` shown in clear.
- **Backend-only OAuth secrets**: `oauth_client_secret` and `oauth_refresh_token` are **never** whitelisted to any container artifact — the backend performs the refresh and injects only the short-lived access token, exactly like Google `oauth_credentials` (`refresh_token` / `client_secret` stay on the backend).
- **Producer ACL enforcement**: who can reach whose agent is governed by the existing `mcp_connector` user-ID ACL (`allowed_user_ids`) + `MCPTokenVerifier`. The agent2agent direct token is connector-scoped; cross-connector use is rejected by the verifier. The consumer's traffic runs **under the connector owner's identity** (existing direct-token semantics) — important: the producer owner authorizes which users may consume, not which agents.
- **Connect-helper ownership checks**: creating a `MCP_PROVIDER` credential from an agent2agent connector requires the caller to be in the producer connector's ACL (or owner/superuser). Mirrors `agent_api`'s up-front consumer-ownership validation (`AgentApiTokenError(403/404)` before any mint).
- **External MCP egress**: arbitrary external MCP URLs are an SSRF surface. The backend OAuth/DCR client must apply the same egress hygiene used elsewhere (no internal-network targets for the *backend-initiated* DCR/refresh calls; document a `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` config defaulting to false in production). The agent container itself making MCP calls is consistent with existing container egress.
- **Foreign-install / agent-user degradation**: consumer installs are **use-only for every role** (memory: foreign installs are use-only). An `agent-user` who installs a bundle that ships an `MCP_PROVIDER` connection credential can use it but not edit/share it. Producer-side connector creation requires `agent-developer` (it exposes an agent). The Sharing card is hidden for `agent-user` (existing role gating in `CredentialSharing`).
- **SSRF / token leakage in prompts**: the MCP endpoint URL appears in the building prompt summary (safe); the token never does (redacted).

---

## Backend Implementation

### API Routes

**Producer (extend existing connector routes — `backend/app/api/routes/mcp_connectors.py`)**

No new endpoints strictly required; `is_agent_to_agent` rides the existing create/update payloads. Optionally add a convenience endpoint for the consumer picker:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/mcp-providers/discoverable-agents` | List platform agents that expose an agent2agent connector the **current user** is allowed to consume (owner or in `allowed_user_ids`). Returns `{ agent_id, agent_name, connector_id, ui_color_preset, mode }[]`. Drives the consumer "Connect MCP Provider → platform agent" picker. Excludes the consumer's own agent when a consumer agent is supplied. |

**Consumer connect helper + management (new router — `backend/app/api/routes/mcp_providers.py`, prefix `/api/v1/mcp-providers`)**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/connect/agent` | Connect to a platform agent2agent connector. Body: `{ connector_id, consumer_agent_id?, modes?, label? }`. Resolves the connector, ACL-checks the caller, mints (or reuses) a connector-scoped direct token via `MCPDirectTokenService`, builds `endpoint_url`, creates an `MCP_PROVIDER` credential `{auth_mode: "agent2agent", target_*}`, links it to `consumer_agent_id` if given (immediate sync). Returns `MCPProviderConnectionResponse`. Mirrors `agent_api`'s `/connect`. |
| `POST` | `/connect/external` | Add an arbitrary external MCP server. Body: `{ endpoint_url, transport, auth_mode ("fixed_token"\|"oauth_dcr"\|"none"), token?, consumer_agent_id?, modes?, label? }`. For `fixed_token`/`none`: create the credential immediately. For `oauth_dcr`: create the credential in `awaiting_auth` and return an authorize URL (see OAuth routes). |
| `GET` | `/{credential_id}/oauth/authorize` | (oauth_dcr) Begin DCR + authorization. Performs DCR against the target AS, stores `oauth_client_*`, builds the authorization URL (PKCE), returns it (frontend opens browser). CSRF state token like Google OAuth. |
| `GET` | `/oauth/callback` | (oauth_dcr) Authorization-code callback. Validates state, exchanges code (PKCE) for tokens, stores `token`/`oauth_refresh_token`/`oauth_token_expires_at` encrypted, triggers credential-updated sync, redirects to credential detail page. |
| `POST` | `/{credential_id}/oauth/reauthorize` | Re-run the authorization flow (revoked refresh token, scope change). |
| `GET` | `/{credential_id}/status` | Derived connection status (`connected`/`awaiting_auth`/`expired`/`error`), endpoint, auth_mode, modes, target agent (if agent2agent). Owner-only. Drives the credential detail panel. |
| `POST` | `/{credential_id}/test` | Best-effort connectivity probe: open an MCP `initialize` + `tools/list` against the endpoint with the current token; return tool names or an error. (Backend-side; the same egress hygiene applies.) |

Disconnect = **delete the `MCP_PROVIDER` credential** through the existing `DELETE /credentials/{id}` (blast-radius gate applies; cascade-deletes the bound producer direct token if OQ-2 chooses binding). No bespoke disconnect route — consistent with `agent_api`'s "delete the credential to revoke."

All routes use `SessionDep`, `CurrentUser`. Ownership/ACL checks return `404` on non-owner of the credential (no existence leak), `403` when the caller lacks producer-connector ACL.

### Service Layer

**`backend/app/services/mcp_providers/mcp_provider_service.py` — `MCPProviderService`** (new domain folder `services/mcp_providers/`)

- `connect_to_agent(session, user, connector_id, consumer_agent_id, modes, label, is_superuser)` → `MCPProviderConnectionResponse` — ACL-check; mint/reuse direct token via `MCPDirectTokenService.create_token`; build `endpoint_url` from `MCP_SERVER_BASE_URL` + connector id; create `MCP_PROVIDER` credential (workspace-stamped consumer-first, like `agent_api`); optional consumer link + sync. Raises `MCPProviderError(403/404/400)`.
- `connect_to_external(session, user, endpoint_url, transport, auth_mode, token, consumer_agent_id, modes, label)` → response — validate URL; for `fixed_token`/`none` create credential immediately; for `oauth_dcr` create `awaiting_auth` credential and hand off to `MCPProviderOAuthService`.
- `get_status(session, credential_id, user, is_superuser)` → `MCPProviderStatus` — derive lifecycle state; resolve target agent name (agent2agent).
- `list_discoverable_agents(session, user, consumer_agent_id)` → list — query `mcp_connector` where `is_agent_to_agent=true AND is_active=true` and the user is owner or in `allowed_user_ids`.
- `build_endpoint_url(connector_id)` → `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`.
- Reuses `CredentialsService.create_credential` / `delete_credential` / link helpers (do not duplicate credential CRUD).

**`backend/app/services/mcp_providers/mcp_provider_oauth_service.py` — `MCPProviderOAuthService`**

- `discover_authorization_server(endpoint_url)` → AS metadata — fetch `/.well-known/oauth-protected-resource` then `/.well-known/oauth-authorization-server` (RFC 9728 / RFC 8414), the same discovery chain our *own* AS publishes. Reuse the exact contract from `docs/application/mcp_integration/agent_mcp_architecture.md`.
- `register_client(as_metadata, redirect_uri, resource)` → `{client_id, client_secret}` — DCR (RFC 7591) `POST {registration_endpoint}`.
- `build_authorization_url(credential, as_metadata, state, code_challenge)` → URL (PKCE S256).
- `exchange_code(credential, code, code_verifier)` → tokens — `POST {token_endpoint}` grant=authorization_code; store `token`/`oauth_refresh_token`/`oauth_token_expires_at` encrypted; fire credential-updated.
- `refresh_access_token(session, credential)` → updated credential — `POST {token_endpoint}` grant=refresh_token; update `token`/`expires_at`; on failure set status `error` (graceful, like Google refresh).
- CSRF state store: in-memory with 10-min TTL keyed by random nonce (reuse the Google OAuth state pattern in `services/credentials/`).

**Pre-stream refresh integration** (`services/credentials/credentials_service.py` OAuth pre-stream hook): extend the existing "refresh OAuth tokens expiring within 600s before stream" loop to also handle `MCP_PROVIDER` rows with `auth_mode="oauth_dcr"`, delegating to `MCPProviderOAuthService.refresh_access_token`. This is the same mechanism Google OAuth credentials use — no new trigger.

**Producer reuse**: `MCPDirectTokenService.create_token` already mints a connector-scoped direct token. If OQ-2 picks per-install binding, add `credential_id` to the mint call and to `MCPToken`. The verifier (`MCPTokenVerifier`) already accepts `token_type="direct"` — no change needed for the producer to accept agent2agent traffic.

### Credential pipeline changes (`credentials_service.py`)

- `SENSITIVE_FIELDS["mcp_provider"] = ["token", "oauth_client_secret", "oauth_refresh_token"]`.
- `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] = []` — never written to `credentials.json`.
- New method `collect_mcp_provider_manifest(session, agent_id, mode)` → `list[{key, url, transport, headers}]` — for each `MCP_PROVIDER` credential linked to the agent, decrypt, filter by `mcp_mode_<mode>`, build `{ key: "cinna_mcp_<credential_id>", url: endpoint_url, transport, headers: {Authorization: "Bearer <token>"} if token }`. Called by env config generation (both engines).
- URL rewrite: agent2agent endpoint URLs use `MCP_SERVER_BASE_URL` (public, but must be **container-reachable** — see OQ-4: in many deployments the public MCP URL is not reachable from inside the agent network; we may need an `MCP_SERVER_CONTAINER_URL` rewrite analogous to `agent_api`'s `_rewrite_agent_api_urls_for_env`).

### Env-core injection (host-side config generation)

- **OpenCode** (`environment_lifecycle._generate_opencode_config_files`): after building `mcp_bridge_servers`, call `collect_mcp_provider_manifest(session, agent_id, mode)` and merge each entry into the `"mcp"` dict as `{ "cinna_mcp_<id>": { "type": "remote", "url": <url>, "headers": {...}, "enabled": true } }`. Namespacing prevents collision with bridge servers and plugin MCP servers (same convention as the plugin path's `plugin_<...>` keys).
- **Claude Code** (`claude_code_sdk_adapter.py` `options.mcp_servers`): env-core needs the per-mode manifest at session start. Mirror the plugin path: expose a container-side helper `get_user_mcp_servers_for_mode(mode)` (reads a manifest the backend syncs into the env, analogous to `settings.json` for plugins), and merge HTTP/SSE server configs into `options.mcp_servers`.
- **Transport of the manifest to the container**: two options —
  - (A) **Regenerate config files on sync** (no new route): include the manifest in `_generate_opencode_config_files` (already runs on create/start/rebuild) and write a `user_mcp.json` per mode for Claude Code to read. Requires a config regeneration + restart on credential change (heavier).
  - (B) **New env-core route** `POST /config/mcp-servers` (mirror `POST /config/plugins`): backend pushes the manifest live on credential change without full regen; env-core writes the per-mode runtime MCP config and the next session picks it up. Lighter, matches the plugin "like libraries" model.
  - **Recommended: (B)** for live updates + (A) baseline on create/rebuild, exactly how plugins do both (`_setup_new_container` baseline + `set_plugins` live). See OQ-5.

### Background tasks

- **Pre-stream OAuth refresh** (existing hook, extended) — synchronous, before stream, graceful on failure.
- **Optional proactive refresh cron** (future): a daily/hourly job refreshing `oauth_dcr` tokens nearing expiry across all `MCP_PROVIDER` credentials, modeled on the model-discovery cron (single-leader via Postgres advisory lock). Out of scope for MVP — pre-stream refresh suffices.

---

## Frontend Implementation

### Producer: 4th sub-tab in MCP Connectors card

`frontend/src/components/Agents/McpConnectorsCard.tsx` (rendered in `AgentIntegrationsTab.tsx`). Today the card lists connectors with create/edit dialogs. Add a **sub-tab strip** with a 4th tab **"Agent to Agent MCP Connector"** alongside the existing MCP-client connector management. This sub-tab:
- Lists connectors where `is_agent_to_agent=true`.
- Create dialog: name, mode (conversation/building), `UserAllowlistPicker` for `allowed_user_ids` (who may consume), `allow_token_access` forced on (a2a needs a direct token). On create it mints the direct token automatically (reuse `McpDirectTokensManager` plumbing or auto-generate one token labeled "agent2agent").
- Shows the consuming side hint ("Share this with users; their agents can connect via Connect MCP Provider").
- Reuses `UserAllowlistPicker` (server-search, fallbackLabel per caller — memory: user-search sharing pickers).

### Consumer: "Connect MCP Provider" button + dialogs

Mirror the `agent_api` consumer surface:
- `frontend/src/components/Credentials/ConnectMcpProviderDialog.tsx` — a dialog with two modes:
  - **Connect to a platform agent**: an agent picker (reuse `AgentSelectorDialog`) fed by `GET /mcp-providers/discoverable-agents`; on select → `POST /connect/agent`.
  - **Add external MCP server**: form (endpoint URL, transport select, auth mode select [None / Fixed token / OAuth (DCR)], token field shown for fixed_token, mode checkboxes conversation/building). On submit → `POST /connect/external`. For `oauth_dcr`, the response carries an authorize URL → open browser → callback → credential becomes `connected`.
- Entry points (mirror `agent_api`):
  - `frontend/src/components/Credentials/AddCredential.tsx` — "Connect MCP Provider" item in the add-credential picker.
  - `frontend/src/components/Agents/AgentCredentialsTab.tsx` — per-agent "Connect MCP Provider" button (passes `consumer_agent_id`).
- `frontend/src/components/Credentials/credentialTypes.ts` — `mcp_provider` display meta (icon/label/badge); not offered in the manual type picker (created only via the helper, like `agent_api`).

### Credential detail page (`frontend/src/routes/_layout/credential/$credentialId.tsx`)

New `mcp_provider` branch (mirror `agent_api`'s three-card layout):
1. **Basic Information** — editable `name`, `notes`, **per-mode toggles** (`mcp_mode_conversation` / `mcp_mode_building`); never shows the token.
2. **Connection panel** (`McpProviderConnectionView.tsx`) — endpoint URL, transport, auth mode, target agent Bot badge (agent2agent), status badge (`connected`/`awaiting_auth`/`expired`/`error`), a **Reauthorize** button for `oauth_dcr`, a **Test** button.
3. **Sharing** (`CredentialSharing`) — role-gated (hidden for `agent-user`). Template-sharing card omitted (a connection has no user-fillable private fields, same reasoning as `agent_api`).

### Global Credentials view (`frontend/src/routes/_layout/credentials.tsx`)

`MCP_PROVIDER` credentials join the **"Automatic Credentials"** section (derived from `type` being a connection type — extend the existing `type === "agent_api"` partition to also include `mcp_provider`), with an explainer ("Connections created by 'Connect MCP Provider'.").

### State Management (React Query)

- `["mcp-providers", "discoverable-agents", consumerAgentId]` — picker source.
- `["mcp-provider-status", credentialId]` — connection status (live badge).
- Connect mutations invalidate `["credentials"]` + the producer's `["mcp-connectors", agentId]`.
- OAuth callback success invalidates `["mcp-provider-status", credentialId]` + `["credentials"]`.
- Reuse existing `["credentials", workspaceFilter]` for the lists.

### User flows / states

- **Empty external form**: hint text describing the difference between fixed token and OAuth.
- **OAuth awaiting_auth**: amber "Authorization required" with an Authorize button; opening the browser; on return, status flips to `connected`.
- **expired**: amber "Token expired — will refresh on next use" (pre-stream refresh handles it); Reauthorize available if refresh fails.
- **error**: red banner with the last refresh/connect error + Reauthorize/Test.
- **Loading/empty**: standard skeletons + empty-state cards consistent with `McpConnectorsCard` / `AgentApiConnectionView`.

### Client regeneration

After backend route additions, regenerate the OpenAPI client: `bash scripts/generate-client.sh` (or `source ./backend/.venv/bin/activate && make gen-client`). New services: `McpProvidersService`. Frontend uses generated types from `@/client` (never hand-edit `src/client/`).

---

## Database Migrations

Single Alembic head off the current `head = 2ca38822e945` (verified via `alembic heads`). Hand-trim autogen drift (the project's known gotcha):
- **Enum**: `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'mcp_provider'` (non-transactional; split into its own migration if PG rejects mixing with DDL, following `aa44agentapi04`).
- **`mcp_connector`**: `+ is_agent_to_agent BOOL NOT NULL DEFAULT false` (btree not needed).
- **`credential`**: `+ mcp_mode_conversation BOOL NOT NULL DEFAULT true`, `+ mcp_mode_building BOOL NOT NULL DEFAULT true`.
- **`mcp_token`**: `+ credential_id UUID NULL REFERENCES credential(id) ON DELETE CASCADE` + btree index on `credential_id` (per-connection bound token — RD-2).

Downgrade: drop the added columns + index; leave the enum value (PG cannot drop enum values safely — matches all prior enum migrations). Apply via `make migrate`; generate via `make migration` then review/trim.

---

## Error Handling & Edge Cases

- **Producer connector deleted while consumers exist**: existing `mcp_connector` delete cascades to its tokens. Consumer `MCP_PROVIDER` credentials then point at a dead endpoint → next call fails; status probe returns `error`. Surface via the credential status badge. (No FK from credential → connector, since cross-user.)
- **Consumer credential deleted**: blast-radius gate (Tier 0/1/2) applies via the existing `DELETE /credentials/{id}`. Cascade-deletes the bound per-connection direct token (RD-2), revoking that consumer only.
- **OAuth refresh failure**: graceful — logged, status `error`, stream still proceeds with the stale/empty token (the MCP server returns 401, the agent sees a failed tool). Reauthorize fixes it. Mirrors Google refresh graceful degradation.
- **DCR not supported by target**: `register_client` 404/400 → surface "This server does not support Dynamic Client Registration; use a fixed token instead."
- **SSRF / unreachable external URL**: `connect/external` and `/test` validate reachability + host policy; clear error.
- **Container can't reach endpoint** (agent2agent public URL not routable from the agent network): the `MCP_SERVER_CONTAINER_URL` rewrite (RD-4) swaps the endpoint netloc on env sync; `/test` surfaces any residual connectivity error.
- **Mode toggles both off**: validation — at least one mode must be enabled, else the credential is inert (warn in UI).
- **Token redaction leak**: ensure the manifest path is the *only* place the token reaches the container, and the SDK config file is `0o600` (same as embedded LLM keys).
- **Shared credential, recipient's agent injects it**: works identically to owned (recipient links it, env sync collects it) — `CredentialShare` recipients can use but not view the token.

---

## UI/UX Considerations

- **Status colors**: green `connected`, amber `awaiting_auth`/`expired`, red `error` — consistent with OAuth credential badges.
- **Per-mode chips**: "Conversation" / "Building" pills on the credential card showing which modes inject the provider.
- **Copyable endpoint URL** on the producer a2a sub-tab and the consumer connection panel.
- **Bot badges** for the target agent (agent2agent), using `ui_color_preset` (same as `agent_api` connections).
- **Help text** distinguishing the three integration kinds in the MCP Connectors card: external-client MCP connector (existing), App MCP route, Identity, and now Agent-to-Agent.
- **Onboarding hint** on the external form clarifying fixed-token vs OAuth/DCR.

---

## Integration Points

- **MCP integration** — reuse `mcp_connector`, `MCPDirectTokenService`, `MCPTokenVerifier`, `MCPServerRegistry`; new `is_agent_to_agent` flag.
- **Agent credentials / sharing** — new `MCP_PROVIDER` type rides sync/whitelist/redaction/`CredentialShare`; new whitelist/redaction/per-mode entries; deletion blast-radius gate applies.
- **OAuth credentials** — DCR/refresh modeled on Google OAuth pre-stream refresh + `event_credential_updated`.
- **Env-core / multi-SDK** — per-mode MCP injection into `opencode.json` `"mcp"` and Claude Code `options.mcp_servers`; new `POST /config/mcp-servers` env-core route (mirror `/config/plugins`).
- **Agent plugins** — the plugin-declared-MCP-server merge (`get_opencode_plugin_artifacts`, namespaced keys) is the direct template for credential-declared MCP servers.
- **`agent_api`** — producer/consumer + connect-helper + connection-is-a-credential + Automatic Credentials grouping + workspace-stamp + sharing pattern.
- **API client regeneration** — `bash scripts/generate-client.sh` after route additions.
- **Bundles** — agent2agent connection credentials can be publisher-provided (PBP) in bundles (one-shared-token model, same trade-offs as `agent_api` PBP); per-install token isolation is future work.

---

## Phase Breakdown (each independently reviewable)

1. **Models + migration** — `is_agent_to_agent` on `mcp_connector`; `CredentialType.MCP_PROVIDER`; `mcp_mode_*` on `credential`; optional `mcp_token.credential_id`. Single-head migration; re-export models. Round-trip via API.
2. **Backend producer** — connector create/update accepts `is_agent_to_agent`; `GET /mcp-providers/discoverable-agents`; auto-mint a2a direct token in the convenience flow. Verifier already accepts direct tokens (no change).
3. **Backend consumer** — `MCPProviderService`, `POST /connect/agent`, `POST /connect/external` (fixed_token/none), `GET /{id}/status`, `POST /{id}/test`, delete-via-credential. Whitelist (`[]`) + redaction + `collect_mcp_provider_manifest`.
4. **Env-core injection** — `collect_mcp_provider_manifest`; OpenCode `opencode.json` `"mcp"` merge; Claude Code `options.mcp_servers` merge; baseline on create/rebuild + live `POST /config/mcp-servers` route (mirror plugins). Container-URL rewrite (OQ-4).
5. **OAuth/DCR** — `MCPProviderOAuthService` (discover/register/authorize/exchange/refresh); `/oauth/authorize`, `/oauth/callback`, `/oauth/reauthorize`; pre-stream refresh hook extension; CSRF state.
6. **Frontend** — producer a2a sub-tab; `ConnectMcpProviderDialog` (both flows); credential detail `mcp_provider` branch + connection view + per-mode toggles + status; Automatic Credentials grouping; entry points; client regen.
7. **Tests** — API-only scenario tests (see Testing) + env-core manual/unit where reachable.
8. **Docs** — feature docs under `docs/application/mcp_integration/` (business + tech), README Feature Registry row + glossary entries (`Agent-to-Agent MCP Connector`, `MCP_PROVIDER credential`), integration-point cross-links.

---

## Testing Approach (API-only; see `backend/tests/README.md`)

Backend (scenario-based, API-only, no direct DB):
- Producer: create connector with `is_agent_to_agent=true`; ACL round-trip; direct token minted; discoverable-agents lists it for an allowed user, hides it for a non-allowed user, excludes the consumer's own agent.
- Consumer agent2agent: `POST /connect/agent` creates an `MCP_PROVIDER` credential (`auth_mode=agent2agent`, target_*, token present), workspace-stamped consumer-first; non-ACL caller → 403; producer connector disabled → connect blocked.
- Consumer external fixed_token/none: credential created; token redacted in README; `MCP_PROVIDER` **absent** from `credentials.json`; manifest collector includes it filtered by mode.
- Per-mode: `mcp_mode_building=false` excludes it from the building manifest; both-off validation.
- Sharing: share the credential → recipient links it → manifest collected in recipient's env; revoke → removed.
- Deletion: delete credential → (if bound) producer direct token cascade-deleted → producer rejects the old token (401 at verifier); blast-radius gate honored.
- OAuth/DCR: discovery + DCR + code exchange (stub AS); refresh updates token + expiry; refresh failure → status `error`, stream proceeds; reauthorize re-runs flow.
- Status/test endpoints: owner-only (404 non-owner); status derives correct lifecycle states.
- Manifest injection (with `EnvironmentTestAdapter` stubs): collector output shape; namespaced keys; redaction never reaches `credentials.json`.

Env-core (manual / unit where reachable, like `agent_api`'s in-container gap): OpenCode `"mcp"` merge, Claude Code `options.mcp_servers` merge, `0o600` config, container-URL rewrite.

---

## Future Enhancements (Out of Scope)

- Per-install token isolation for shared agent2agent connections (same gap as `agent_api` PBP).
- Proactive background OAuth-refresh cron (pre-stream refresh suffices for MVP).
- MCP registry browser inside the platform (cinna-desktop-style registry discovery) for one-click external-server add.
- Per-tool allow/deny on a consumed MCP server (narrowing which tools the agent may call).
- stdio (local) MCP providers — out of scope; this feature is remote MCP only.
- Usage analytics / per-connection call counts.

---

## Summary Checklist

**Backend**
- [ ] Add `CredentialType.MCP_PROVIDER`; `is_agent_to_agent` on `mcp_connector`; `mcp_mode_conversation`/`mcp_mode_building` on `credential`; optional `mcp_token.credential_id`. Re-export models.
- [ ] Single-head Alembic migration (enum add possibly split; trim autogen drift).
- [ ] `GET /mcp-providers/discoverable-agents` (producer-side discovery for consumer picker).
- [ ] `MCPProviderService` + `POST /connect/agent`, `POST /connect/external`, `GET /{id}/status`, `POST /{id}/test`.
- [ ] `MCPProviderOAuthService` + `/oauth/authorize`, `/oauth/callback`, `/oauth/reauthorize`; CSRF state; pre-stream refresh hook extension.
- [ ] Credential pipeline: `SENSITIVE_FIELDS["mcp_provider"]`, `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"]=[]`, `collect_mcp_provider_manifest`, container-URL rewrite.

**Agent-env**
- [ ] `collect_mcp_provider_manifest` merge into OpenCode `opencode.json` `"mcp"` (namespaced keys, baseline on create/rebuild).
- [ ] Claude Code `options.mcp_servers` merge via `get_user_mcp_servers_for_mode`.
- [ ] `POST /config/mcp-servers` env-core route (live push, mirror `/config/plugins`).
- [ ] Per-mode `0o600` runtime MCP config.

**Frontend**
- [ ] Producer "Agent to Agent MCP Connector" sub-tab in `McpConnectorsCard` (UserAllowlistPicker, auto-mint direct token).
- [ ] `ConnectMcpProviderDialog` (platform-agent flow + external flow incl. OAuth/DCR browser hand-off).
- [ ] Credential detail `mcp_provider` branch: Basic Info + per-mode toggles, `McpProviderConnectionView` (status/test/reauthorize), Sharing (role-gated).
- [ ] Entry points in `AddCredential` + `AgentCredentialsTab`; `credentialTypes.ts` meta; Automatic Credentials grouping.
- [ ] Regenerate client (`bash scripts/generate-client.sh`).

**Testing & validation**
- [ ] Producer ACL + discoverability; consumer connect (both flows); per-mode injection; redaction (token never in `credentials.json`); sharing; deletion/revocation; OAuth/DCR + refresh; status/test owner-only.
- [ ] Manual env-core verification of SDK MCP injection for both engines.

**Docs**
- [ ] Business + tech docs under `docs/application/mcp_integration/`; README Feature Registry row + glossary; integration cross-links.

---

## Resolved Decisions

All decisions below are **final** (signed off). Delivery scope is **all phases in one go**, including the OAuth/DCR external-MCP path.

- **RD-1 — Connector model: extend.** Extend `mcp_connector` with the `is_agent_to_agent` flag and reuse the existing RS stack (`MCPServerRegistry`, `MCPTokenVerifier`, `MCPDirectTokenService`) and the user-ID ACL. No new connector table — agent2agent and external-client connectors share one table (identical RS behaviour).

- **RD-2 — Per-connection bound token.** Each agent2agent connection mints a distinct `mcp_token` (`token_type="direct"`) bound to its consumer `MCP_PROVIDER` credential via `mcp_token.credential_id` FK (`ON DELETE CASCADE`), mirroring `agent_api_token.credential_id`. Deleting the consumer credential = revoke **that consumer only**, leaving other consumers of the same producer connector unaffected.

- **RD-3 — Backend-side DCR + OAuth refresh.** DCR client registration and OAuth refresh run on the **backend** (`MCPProviderOAuthService`). `client_secret` and `refresh_token` never leave the backend; only the short-lived access token is injected into the container. Reuse — extend — the existing pre-stream refresh hook (the same mechanism Google `oauth_credentials` use).

- **RD-4 — Container-reachable endpoint rewrite.** Add an `MCP_SERVER_CONTAINER_URL` config (the internal MCP origin) and rewrite the endpoint netloc on env sync for `auth_mode=agent2agent`, analogous to `agent_api`'s `AGENT_ENV_BACKEND_URL` / `_rewrite_agent_api_urls_for_env`. The stored credential and UI keep the public URL; only the env-synced manifest copy is rewritten.

- **RD-5 — Live manifest transport + persisted baseline.** Add a live `POST /config/mcp-servers` env-core route (mirroring `POST /config/plugins`) for in-place updates on credential change, plus a persisted baseline manifest applied on create/rebuild (mirroring the plugin manifest approach). Shipping the env-core route requires an env rebuild, as with the plugins work.

- **RD-6 — SSRF / egress guard.** All backend-initiated calls to external MCP servers (DCR registration, OAuth refresh, `/test`) go through an SSRF/egress guard that blocks internal/link-local/private ranges and enforces an `http`/`https` scheme allowlist. A `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` config (default false in prod) provides a self-hosted override.

- **RD-7 — Role gating.** Creating an agent2agent connector (which exposes an agent over MCP) requires `agent-developer`. Consuming a connection — including installing/using a shared one — is **use-only** and available to `agent-user`. The Sharing card stays hidden for `agent-user` (existing role gating).
