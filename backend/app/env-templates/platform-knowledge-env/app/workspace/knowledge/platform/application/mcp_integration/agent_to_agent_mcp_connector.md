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

### Connection = Credential (no manual token management)

There is **no manual token management**. A *connection* between two agents **is** the `mcp_provider` credential. The token lives inside its encrypted `credential_data` and is never shown or managed by the user. **Deleting the credential is the only way to disconnect** — it cascade-deletes the bound direct token (agent2agent only), revoking that consumer only without affecting other consumers of the same producer connector.

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

### Per-Consumer Token Isolation

Each agent2agent connection mints a **distinct** `mcp_token` (`token_type="direct"`) bound to its consumer `mcp_provider` credential via `mcp_token.credential_id` FK (`ON DELETE CASCADE`), mirroring `agent_api_token.credential_id`. Deleting the consumer credential revokes that consumer only; other consumers of the same connector are unaffected (RD-2).

---

## Consumer Side

### "Connect MCP Provider" — the only way to connect

Connecting is a single action, surfaced from the **consumer** agent's Credentials tab (and the global "Add Credential" picker). A dialog offers two paths (platform agent or external server) and creates an `mcp_provider` credential pre-filled with the connection details, linked immediately to the consumer agent.

**Workspace stamp**: like `agent_api`, the credential is stamped with `user_workspace_id` consumer-first: if a consumer agent is given, it inherits that agent's workspace; otherwise the producer agent's workspace; otherwise the default workspace.

**Modes**: each credential carries `mcp_mode_conversation` and `mcp_mode_building` toggles (default both on) which control which SDK modes receive the MCP server. At least one must remain on — both off leaves the credential inert.

### Global Credentials View

`mcp_provider` credentials appear in the **Automatic Credentials** section in `/credentials` (alongside `agent_api`). Their detail page shows:
- Editable name and notes, plus per-mode toggles (`mcp_mode_conversation` / `mcp_mode_building`).
- A connection panel with endpoint URL, transport, auth mode, target agent Bot badge (agent2agent), and a derived status badge.
- A **Test** button that probes the endpoint (`MCP initialize` + `tools/list`) and reports the available tool names or a connectivity error.
- A **Reauthorize** button for `oauth_dcr` credentials when the refresh token is revoked or scopes change.
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

Disconnect options:
- Delete the `mcp_provider` credential (blast-radius gate applies) — cascade-deletes the bound direct token, revoking that consumer only.
- Revoke a `CredentialShare` — cuts that recipient's access without deleting the token.

---

## Error Cases and Edge Handling

- **Producer connector deleted while consumers exist**: consumer `mcp_provider` credentials then point at a dead endpoint. Next session start the MCP server is included in the config but returns errors; the status probe returns `error`. Reauthorize does not help — the credential should be deleted.
- **Mode toggles both off**: validation at the service layer blocks this (`400`); the UI warns if both are deselected.
- **DCR not supported by target**: the backend surfaces "This server does not support Dynamic Client Registration; use a fixed token instead."
- **Connectivity test fails**: the `/test` endpoint returns `{ ok: false, error: … }` with the cause; no state is changed on failure.
- **Shared credential across users**: recipient links the credential, env sync collects it, manifest is injected into their containers — identical to the owner's experience.

---

## Integration Points

- **[MCP Integration](agent_mcp_architecture.md)** — producer side reuses `mcp_connector`, `MCPDirectTokenService`, `MCPTokenVerifier`, `MCPServerRegistry`; adds `is_agent_to_agent` flag.
- **[MCP Connector Setup](mcp_connector_setup.md)** — the agent-to-agent sub-tab is a fourth panel in the connector management card alongside the existing setup flows.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — new `MCP_PROVIDER` type rides the sync/whitelist/redaction/`CredentialShare` pipeline; new credential pipeline entries.
- **[OAuth Credentials](../../agents/agent_credentials/oauth_credentials.md)** — DCR/refresh modeled on Google OAuth pre-stream refresh + `event_credential_updated`.
- **[Agent Environment Core / Multi-SDK](../../agents/agent_environment_core/multi_sdk_tech.md)** — per-mode MCP injection into OpenCode `"mcp"` and Claude Code `options.mcp_servers`; new env-core `POST /config/mcp-servers` route.
- **[Agent Plugins](../../agents/agent_plugins/agent_plugins_tech.md)** — the plugin-declared-MCP-server merge is the direct template for credential-declared MCP servers; namespaced keys prevent collision.
- **[Agent REST API](../../agents/agent_api/agent_api.md)** — producer/consumer + connect-helper + connection-is-a-credential + Automatic Credentials grouping + workspace-stamp + sharing pattern is the same architecture.

- **[Account CLI Workspace](../../application/cinna_cli_integration/account_cli_workspace.md)** — `cinna connect mcp --producer P --consumer C` wraps `MCPProviderService.connect_to_agent` via `POST /api/v1/cli/account/connect/mcp`; `GET /account/connect/mcp/discoverable` is the account-token-accessible passthrough that maps producer agent name → connector_id.

---

## Known Gaps and Future Work

- **Per-install token isolation for shared agent2agent connections**: publisher-provided `mcp_provider` credentials use the one-shared-token model (same gap as `agent_api` PBP).
- **Proactive OAuth refresh cron**: a background job refreshing expiring tokens across all `oauth_dcr` credentials. Pre-stream refresh covers most cases for MVP.
- **MCP registry browser**: one-click add from a marketplace of known external MCP servers.
- **Per-tool allow/deny**: narrowing which tools from a consumed MCP server the agent may call.
- **stdio (local) MCP providers**: this feature is remote MCP only.

---

*Last updated: 2026-06-08*
