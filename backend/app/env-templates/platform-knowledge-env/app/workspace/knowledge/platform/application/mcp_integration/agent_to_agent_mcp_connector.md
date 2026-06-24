# Agent-to-Agent MCP Connector

## Purpose

Let one platform agent connect to another platform agent — or to an arbitrary external MCP server — as a first-class MCP provider, so the SDK receives the connection as a live MCP server rather than a credential file. The connected MCP server is injected directly into the SDK runtime (OpenCode `opencode.json` `"mcp"` block / Claude Code `options.mcp_servers`), per-mode, exactly like a plugin-declared MCP server, so the agent's tools include everything the remote server exposes without any extra code.

This mirrors the `agent_api` producer/consumer architecture: **a connection is a credential** — created by a one-click helper, it rides the existing credential sync / whitelist / redaction / sharing pipeline. The difference from `agent_api` is the materialization target: instead of writing to `credentials.json`, the `mcp_provider` credential is translated into an MCP server entry in the SDK runtime config.

---

## Core Concepts

### Producer / Consumer Model

```
PRODUCER SIDE                                    CONSUMER SIDE
┌─────────────────────────────────┐             ┌──────────────────────────────────────┐
│ Agent A (owner)                  │             │ Agent B (owner)                       │
│ Integrations → MCP Connectors    │             │ Credentials tab                       │
│   → "Agent to Agent MCP          │             │   → "Connect MCP Provider"            │
│      Connector" sub-tab          │             │       ├─ pick platform agent A        │
│   creates mcp_connector          │             │       └─ add external MCP URL         │
│   (is_agent_to_agent=true)       │             │   creates mcp_provider credential     │
│   + user-ID ACL                  │             │   (endpoint, auth_mode, modes, token) │
└───────────────┬─────────────────┘             └────────────────┬─────────────────────┘
                │ exposes /mcp/{connector_id}/mcp                  │ not a file — SDK MCP config
                ▼                                                  ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │ existing per-connector MCPServer (MCPServerRegistry, MCPTokenVerifier)       │
   │ + env-core: collect_mcp_provider_manifest → POST /config/mcp-servers        │
   │   → user_mcp.json (0o600) → SDK merge (cinna_mcp_<credential_id> key)       │
   └─────────────────────────────────────────────────────────────────────────────┘
```

### Two Flows, One Credential Type

**Flow A — Platform agent2agent:** connect to another platform agent's agent-to-agent connector. The platform resolves the connector ACL, mints a connector-scoped direct token, and builds the endpoint URL automatically. The token is bound to the credential (cascade-revoke on delete).

**Flow B — External MCP server:** add any HTTP/HTTPS MCP server by URL. Three auth modes:
- `none` — no authentication (public or firewall-protected server).
- `fixed_token` — user supplies a static bearer token.
- `oauth_dcr` — the backend performs Dynamic Client Registration (RFC 7591) + authorization-code exchange (PKCE S256) on the user's behalf; `client_secret` and `refresh_token` never leave the backend.

### Pair-Connection Semantics (agent2agent only)

An agent2agent `mcp_provider` credential is a **strict one-to-one connection** between a specific producer connector and a specific consumer agent. Both sides of the pair are recorded:

- **Producer** — stored in the encrypted blob (`target_agent_id` / `target_connector_id`); always set at connect time.
- **Consumer** — recorded as the first-class column `Credential.mcp_consumer_agent_id` (FK → `agent.id`); set at connect time; visible in the credential detail UI (see "Consumer Side" below).

Consequences of this pairing:

- **Idempotent connect**: connecting the same (producer connector, consumer agent) pair twice returns the existing credential unchanged — no duplicate credential or second token is created.
- **Cannot be re-homed**: linking an agent2agent credential to a different consumer agent is rejected with a `400`. Each pair owns exactly one credential.
- **Auto-cleanup on disconnect** (see "Disconnecting" below).

This is a deliberate divergence from the `agent_api` twin, which does not enforce a one-to-one consumer binding. External/manual `mcp_provider` credentials (`none` / `fixed_token` / `oauth_dcr`) are NOT subject to pair semantics — they remain freely linkable to multiple agents and shareable.

### Connection = Credential (no manual token management)

There is **no manual token management**. A *connection* between two agents **is** the `mcp_provider` credential. The token lives inside its encrypted `credential_data` and is never shown or managed by the user.

### Not Written to `credentials.json`

Unlike every other credential type, `mcp_provider` credentials are **never written to `credentials.json`** in the container. The token cannot be read from `credentials.json`. Instead, the platform collects all linked `mcp_provider` credentials into a per-mode manifest and pushes it into the SDK runtime config (a separate `user_mcp.json` file at `0o600`). This is a deliberate security boundary: the token is only where it must be (the SDK transport layer), not in the general-purpose credential file that any script in the agent workspace can read.

---

## Producer Side

### Creating an Agent-to-Agent Connector

An agent owner sets up a connector from their agent's **Integrations → MCP Connectors** card, which now has a **"Agent to Agent MCP Connector"** sub-tab (alongside external-client, App MCP, and Identity tabs). This sub-tab:

1. Lists connectors flagged `is_agent_to_agent=true` for this agent.
2. Creates new connectors with a name, mode (conversation/building), and a `UserAllowlistPicker` for `allowed_user_ids` — which users are permitted to consume this connector.
3. Automatically enables `allow_token_access` so the consumer connect helper can mint a direct token without an OAuth round-trip.
4. Shows the MCP server URL (`{MCP_SERVER_BASE_URL}/{connector_id}/mcp`) as a copyable hint.

**Creating this connector requires the `agent-developer` role** (it exposes an agent). The connector reuses the full MCP connector infrastructure — `MCPServerRegistry`, `MCPTokenVerifier`, `MCPDirectTokenService` — unchanged (RD-1).

### Editing an Agent-to-Agent Connector

The edit dialog for an `is_agent_to_agent=true` connector shows only the same fields as the create form: name, mode, and `UserAllowlistPicker`. The "Allow token access" switch and "MCP Server URL" fields (shown for external connectors) are hidden — `allow_token_access` remains auto-enabled for agent2agent and is not user-editable.

### Per-Consumer Token Isolation

Each agent2agent connection mints a **distinct** `mcp_token` (`token_type="direct"`) bound to its consumer `mcp_provider` credential via `mcp_token.credential_id` FK (`ON DELETE CASCADE`), mirroring `agent_api_token.credential_id`. Deleting the consumer credential revokes that consumer only; other consumers of the same connector are unaffected (RD-2).

---

## Consumer Side

### "Connect MCP Provider" — the only way to connect

Connecting is a single action, surfaced from the **consumer** agent's Credentials tab (and the global "Add Credential" picker). A dialog offers two paths (platform agent or external server) and creates an `mcp_provider` credential pre-filled with the connection details, linked immediately to the consumer agent.

The **Platform Agent path** does not show a URL field — the endpoint is derived server-side automatically from the connector. The **External server path** shows the endpoint URL input for manually-specified MCP servers.

**Workspace stamp**: the credential is stamped with `user_workspace_id` consumer-first:
- **Platform agent2agent**: if a consumer agent is given, it inherits that agent's workspace; otherwise the producer agent's workspace; otherwise the default workspace (the pair follows the agents).
- **External server**: if a consumer agent is given, it inherits that agent's workspace; otherwise the credential follows the user's **active workspace** (passed from the `/credentials` page filter as `user_workspace_id` on the connect request, ownership-validated server-side), exactly like any manually-created "My Credentials" entry. With no active workspace it lands in the default workspace.

**Modes**: each credential carries `mcp_mode_conversation` and `mcp_mode_building` toggles (default both on) which control which SDK modes receive the MCP server. At least one must remain on — both off leaves the credential inert.

### Credential Categorization (Automatic vs My Credentials)

`mcp_provider` credentials are split across the `/credentials` tabs by **how they are managed**, not merely by type:

- **agent2agent** connections (`mcp_auth_mode == "agent2agent"`) → **Automatic Credentials** (alongside `agent_api`). They are auto-created by the connect helper, pair-bound, and auto-deleted on disconnect — the platform manages them.
- **External / manual** servers (`none` / `fixed_token` / `oauth_dcr`) → **My Credentials**. They are user-created and user-managed (manually linked, shared, edited, and deleted) like any ordinary credential.

The discriminator is a cheap, non-secret column `Credential.mcp_auth_mode` (mirrored out of the encrypted blob) so `CredentialsService.classify_credential_category` can decide the tab without decrypting. Only `agent2agent` rows are "automatic"; everything else (including a manual external provider) is "mine".

### Global Credentials View / Connection Schema

The credential detail page renders the connection as a left-to-right **MCP Client → MCP Server** schema so the user can see exactly where the client is, where the server is, and which modes are involved:

- **Left — MCP Client**: the consumer agent that *uses* the connection (`MCPProviderStatus.consumer_agent`, from `mcp_consumer_agent_id`), shown as an `AgentBadge`; falls back to a neutral "Any linked agent" pill for external/unbound providers. This side owns the editable **per-mode switches** (`mcp_mode_conversation` / `mcp_mode_building`) — the modes in which the client injects the connection.
- **Right — MCP Server**: the producer agent (`target_agent`, agent2agent) as an `AgentBadge`, or an "External server" badge. Shows the live status badge, the endpoint URL (copyable, external only), and — for agent2agent only — a read-only **"Serves mode"** indicator reflecting the producer connector's single served mode (`MCPProviderStatus.connector_mode`, resolved from the bound connector). The auth-mode badge is shown only for external servers (redundant for agent2agent). External servers omit the mode block entirely (an external server has no notion of modes).
- Compact **Test** (probe `MCP initialize` + `tools/list`) and **Reauthorize** (oauth_dcr only) icon buttons sit in the MCP Server box's top-right corner.
- Editable name and notes below the schema.
- The standard **Sharing** card (role-gated: hidden for `agent-user`). Template sharing is not offered — a connection credential has no user-fillable private fields.

### Connection Status

The derived lifecycle state (not stored; computed from the encrypted blob):

| State | Meaning |
|-------|---------|
| `connected` | Token present (or auth mode is `none`). |
| `awaiting_auth` | `oauth_dcr`, DCR done, waiting for the user to complete browser authorization. |
| `expired` | `oauth_dcr`, access token past its expiry; will be refreshed before the next session start. |
| `error` | Last token refresh failed; displayed with the error message; Reauthorize fixes it. |

---

## Runtime: SDK Injection

The `mcp_provider` credential becomes an MCP server in the agent's SDK for each enabled mode. No code change is needed in the agent workspace; the platform handles all of this.

### Delivery Path

1. On credential create/link/update, `CredentialsService.collect_mcp_provider_manifest(agent_id, mode)` builds the per-mode manifest: for each linked `mcp_provider` credential whose `mcp_mode_<mode>` is on, it decrypts, applies the `MCP_SERVER_CONTAINER_URL` rewrite for agent2agent connections (RD-4), and emits `{ key: "cinna_mcp_<credential_id>", url, transport, headers: {Authorization: "Bearer <token>"} }`.
2. `EnvironmentLifecycleManager._sync_mcp_servers_to_environment(...)` calls `adapter.set_mcp_servers(manifest)` (live push, `POST /config/mcp-servers` route in env-core, mirroring `POST /config/plugins`). This is also called on env create/start/rebuild as a baseline (RD-5).
3. Env-core writes the manifest to `mcp/user_mcp.json` (0o600).
4. At session start, each SDK adapter reads the manifest and merges entries under namespaced keys (`cinna_mcp_<credential_id>`) into the SDK config, filtered by mode:
   - **OpenCode**: merged into the `"mcp"` section of `opencode.json` as `{ "type": "remote", "url": …, "headers": {…}, "enabled": true }`.
   - **Claude Code**: merged into `options.mcp_servers` as HTTP/SSE server configs.

### Container URL Rewrite (RD-4)

Agent2agent endpoint URLs use `MCP_SERVER_BASE_URL` (public, configured for external clients). Inside the agent container the public URL may not be routable. For `auth_mode=agent2agent`, `collect_mcp_provider_manifest` swaps the netloc to `MCP_SERVER_CONTAINER_URL` (the internal MCP origin) before writing the manifest — exactly like `_rewrite_agent_api_urls_for_env`. The stored credential and the UI keep the public URL; only the container-synced manifest copy is rewritten.

### OAuth Pre-Stream Refresh (RD-3)

For `oauth_dcr` credentials, `CredentialsService._refresh_expiring_oauth_tokens_before_stream` is extended to also handle `mcp_provider` rows: access tokens expiring within 600 seconds are refreshed by `MCPProviderOAuthService.refresh_access_token` before the session stream begins. `client_secret` and `refresh_token` stay on the backend; only the fresh access token reaches the container manifest. Refresh failure is graceful (logs + `status=error`; the session still starts with the stale/empty token; the agent sees MCP auth errors; Reauthorize fixes it).

---

## Security Model

- **Encryption**: `credential_data` (including `token`, `oauth_client_secret`, `oauth_refresh_token`) encrypted at rest via existing Fernet credential encryption. No new crypto.
- **Never in `credentials.json`**: `AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] = []`. The token only reaches the SDK manifest path.
- **Redacted in prompts**: `SENSITIVE_FIELDS["mcp_provider"] = ["token", "oauth_client_secret", "oauth_refresh_token"]` — the token appears as `***REDACTED***` in any `README.md` or building-prompt credential summary.
- **Backend-only OAuth secrets**: `oauth_client_secret` and `oauth_refresh_token` are never whitelisted to any container artifact; the backend performs refresh and injects only the short-lived access token (mirrors Google `oauth_credentials` pattern).
- **Producer ACL**: who may consume a connector is governed by `mcp_connector.allowed_user_ids` + `MCPConnectorService.check_user_access`. The connect helper checks the caller is in the ACL (or is owner/superuser) before minting any token.
- **Consumer-ownership check**: when `consumer_agent_id` is given, the caller must own that agent — checked before any token is minted (same pattern as `connect_agent_api`).
- **Existence-leak prevention**: non-owner requests return `404` (not `403`) for credential operations; non-ACL callers on connect return `403` (connector exists but they may not consume it).
- **SSRF / egress guard (RD-6)**: all backend-initiated calls to external MCP servers (DCR registration, OAuth refresh, `/test` probe) go through `egress_guard.assert_url_allowed`, which blocks private/loopback/link-local ranges and enforces `http`/`https` scheme. `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS=false` by default in production; set to `true` for self-hosted internal MCP servers.
- **Role gating (RD-7)**: creating an agent2agent connector (exposing an agent over MCP) requires `agent-developer`. Consuming a connection — including installing/using a shared one — is use-only and available to all roles including `agent-user`. The Sharing card is hidden for `agent-user` (existing role gating in `CredentialSharing`).

---

## Sharing

`mcp_provider` credentials support all three sharing modes (user / publisher / template) by riding the existing `CredentialShare` pipeline. Sharing an agent2agent connection is safe because the thing shared is the **narrowed MCP endpoint + token**, not any upstream secret. Cross-user sharing delivers the MCP server to the recipient's agent's SDK just as it would to the owner's.

External/manual providers (`none` / `fixed_token` / `oauth_dcr`) share freely. Agent2agent credentials are paired to a single consumer agent and cannot be re-homed to a different consumer; they are deleted automatically when disconnected (see below).

Disconnect options (external/manual `mcp_provider`):
- Delete the `mcp_provider` credential (blast-radius gate applies) — cascade-deletes the bound direct token (agent2agent only, not applicable here), removing the MCP server from the consumer's SDK config.
- Revoke a `CredentialShare` — cuts that recipient's access without deleting the credential.

Disconnect (agent2agent `mcp_provider` — auto-cleanup):
- **Producer connector deleted**: all agent2agent `mcp_provider` credentials built from that connector are **automatically deleted** (bypass blast-radius gate), their bound direct tokens revoked via cascade, and each affected consumer's environment is synced so the dead MCP server is removed from `user_mcp.json`. No orphaned credentials remain.
- **Credential unlinked from its bound consumer agent**: the credential is **automatically deleted** (no meaning without its pair). The bound direct token is cascade-revoked; the consumer's environment is synced.
- **Unlinking a non-bound agent** (e.g. an extra share-link) or any external/manual provider: plain unlink only — the credential survives (unchanged from current behavior).

> The blast-radius tiered gate still applies to explicit user-initiated `DELETE /credentials/{id}` on manual `mcp_provider` credentials. The auto-cleanup paths on agent2agent credentials bypass the gate intentionally: the connector deletion or consumer unlink is itself the authorization signal. Manual deletion of an agent2agent credential still goes through the gate normally.

---

## Error Cases and Edge Handling

- **Producer connector deleted while consumers exist**: all agent2agent `mcp_provider` credentials built from the connector are **automatically deleted**; consumer environments are synced to remove the dead MCP server. No orphaned credentials or stale `user_mcp.json` entries remain.
- **Connect the same pair twice (agent2agent)**: idempotent — returns the existing credential; no second token or credential is created.
- **Link agent2agent credential to a different consumer agent**: rejected with `400`. The pair binding is immutable once set.
- **Floating agent2agent credential** (created without a consumer, then linked): the first agent linked becomes the bound consumer (`mcp_consumer_agent_id` is set on first link). Linking to a different agent after that → `400`.
- **Consumer agent deleted**: `AgentCredentialLink` rows cascade away; `mcp_consumer_agent_id` is set to `NULL` (FK `ON DELETE SET NULL`). The credential becomes a harmless floating row the owner can delete manually. It does not cause a cascade-delete of the credential.
- **Mode toggles both off**: validation at the service layer blocks this (`400`); the UI warns if both are deselected.
- **DCR not supported by target**: the backend surfaces "This server does not support Dynamic Client Registration; use a fixed token instead."
- **Connectivity test fails**: the `/test` endpoint returns `{ ok: false, error: … }` with the cause; no state is changed on failure.
- **OAuth callback double-submit (single-use state)**: the authorization `state` is single-use — the backend consumes it on the first `POST /mcp-providers/oauth/callback` and exchanges the code. The frontend callback route guards with a `useRef` so the callback fires exactly once per mount; without it, React StrictMode's dev double-invoke fired a second callback whose now-consumed state returned `400`, surfacing a spurious "Authorization failed" even though the first call succeeded. The backend keeps state single-use (anti-replay); the fix is purely on the client to not self-replay.
- **Shared credential across users**: recipient links the credential, env sync collects it, manifest is injected into their containers — identical to the owner's experience. (Agent2agent credentials sharing is subject to the pair constraints above.)
- **Environment built before this feature shipped**: the env-core code lives in the container's `/app/core`, which is a per-environment copy of `app_core_base` refreshed only on rebuild. An environment whose container predates this feature has no `POST /config/mcp-servers` route, so `set_mcp_servers` returns `404`. The push is non-blocking and swallowed (logged as `MCP-provider sync … failed (non-blocking)`), so the credential looks connected but no `cinna_mcp_*` server reaches the SDK and no `user_mcp.json` is written. **Fix: rebuild the consumer's environment** so the current env-core (route + adapter merge) is copied in; the rebuild baseline sweep then writes `user_mcp.json` and the next session injects the server. Symptom-check: `GET /config/mcp-servers` 404 in backend logs for that agent vs `200` for freshly-built agents.

---

## Integration Points

- **[MCP Integration](agent_mcp_architecture.md)** — producer side reuses `mcp_connector`, `MCPDirectTokenService`, `MCPTokenVerifier`, `MCPServerRegistry`; adds `is_agent_to_agent` flag.
- **[MCP Connector Setup](mcp_connector_setup.md)** — the agent-to-agent sub-tab is a fourth panel in the connector management card alongside the existing setup flows.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — new `MCP_PROVIDER` type rides the sync/whitelist/redaction/`CredentialShare` pipeline; new credential pipeline entries.
- **[OAuth Credentials](../../agents/agent_credentials/oauth_credentials.md)** — DCR/refresh modeled on Google OAuth pre-stream refresh + `event_credential_updated`.
- **[Agent Environment Core / Multi-SDK](../../agents/agent_environment_core/multi_sdk_tech.md)** — per-mode MCP injection into OpenCode `"mcp"` and Claude Code `options.mcp_servers`; new env-core `POST /config/mcp-servers` route.
- **[Agent Plugins](../../agents/agent_plugins/agent_plugins_tech.md)** — the plugin-declared-MCP-server merge is the direct template for credential-declared MCP servers; namespaced keys prevent collision.
- **[Agent REST API](../../agents/agent_api/agent_api.md)** — producer/consumer + connect-helper + connection-is-a-credential + Automatic Credentials grouping + workspace-stamp + sharing pattern is the same architecture. **Divergence on disconnect semantics**: `agent_api` records the consumer only via `AgentCredentialLink` (no consumer column, no auto-delete on unlink). Agent2agent MCP provider credentials intentionally diverge — they record the consumer in a dedicated column and auto-delete the credential on any disconnect trigger — because an MCP pair connection is a stricter one-to-one binding than an `agent_api` connection.

- **[Account CLI Workspace](../../application/cinna_cli_integration/account_cli_workspace.md)** — `cinna connect mcp --producer P --consumer C` wraps `MCPProviderService.connect_to_agent` via `POST /api/v1/cli/account/connect/mcp`; `GET /account/connect/mcp/discoverable` is the account-token-accessible passthrough that maps producer agent name → connector_id.

---

## Known Gaps and Future Work

- **Auto-delete when consumer agent itself is deleted**: currently `mcp_consumer_agent_id` is set to `NULL` (FK `ON DELETE SET NULL`) on agent deletion, leaving a harmless orphan credential the owner must delete manually. Auto-deleting the credential on consumer-agent deletion is out of scope.
- **Per-install token isolation for shared agent2agent connections**: publisher-provided `mcp_provider` credentials use the one-shared-token model (same gap as `agent_api` PBP).
- **Pre-existing agent2agent credential backfill**: credentials created before migration `e5f972e7e32e` have `mcp_consumer_agent_id = NULL`. They do not participate in pair-deduplication or auto-delete-on-unlink until rebound. An optional one-time backfill (script or data migration) can set the column from existing `AgentCredentialLink` rows where exactly one link exists.
- **Producer-side pair visibility**: no UI showing which consumer agents are currently connected to a given connector.
- **Proactive OAuth refresh cron**: a background job refreshing expiring tokens across all `oauth_dcr` credentials. Pre-stream refresh covers most cases for MVP.
- **MCP registry browser**: one-click add from a marketplace of known external MCP servers.
- **Per-tool allow/deny**: narrowing which tools from a consumed MCP server the agent may call.
- **stdio (local) MCP providers**: this feature is remote MCP only.

---

*Last updated: 2026-06-24*
