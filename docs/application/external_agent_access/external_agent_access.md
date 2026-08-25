# External Agent Access API

## Purpose

A dedicated REST + A2A surface under `/api/v1/external/` that gives authenticated native clients (Cinna Desktop, future Cinna Mobile) a clean, first-party interface for discovering agents, chatting with them over A2A, and managing their thread history — without the web SPA in the loop.

**As of Phase 5 of the channels & identity unification refactor**, this surface has **two** target kinds, not three. The `"app_mcp_route"` target type — reaching another user's agent through an `AppAgentRoute` — is deleted along with the entire route family; that avenue for reaching a shared agent no longer exists on any surface. What remains: an authenticated user's own personal agents, and the identity contacts they've enabled. **Cinna Desktop and the `cinna-cli` (a separate repo) both consumed the route target kind and will break** — see [Client Impact](#client-impact-cinna-desktop-and-cinna-cli) below.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **External Target** | Any addressable entity the native client can chat with: a personal agent, or an identity contact |
| **Target Type** | One of `"agent"`, `"identity"` — determines which A2A endpoint family to call. `"app_mcp_route"` is **removed** |
| **Agent Card URL** | Absolute URL returned per target pointing at that target's external A2A endpoint; the client fetches the card from this URL and sends messages to the same URL |
| **Soft-hide** | Marking a session as hidden for the calling user without deleting it from the database |
| **Client Attribution** | `client_kind` + `external_client_id` claims in desktop JWTs, stamped into `session_metadata` when a new session is created |

---

## User Stories / Flows

### Launch: Restoring the Thread List

1. Native client authenticates via the Desktop OAuth flow (or existing session token)
2. Client calls `GET /api/v1/external/sessions` (limit/offset) to restore previous conversations
3. Each `ExternalSessionPublic` item carries `target_type` + `target_id` + `agent_card_url`-derivable routing info so the client can navigate directly to any conversation without a full A2A reconnect per thread
4. Client renders the thread picker sorted by `last_message_at DESC`

### Home Screen: Discovering Addressable Targets

1. Client calls `GET /api/v1/external/agents` (optionally with `?workspace_id=` to scope to one workspace)
2. Response contains a `targets` list in **two** ordered sections: personal agents → identity contacts
3. Each target includes `name`, `description`, `entrypoint_prompt`, `example_prompts`, `agent_card_url`, `protocol_versions`, `mcp` (the `cinna.mcp` descriptor — see [cinna.mcp descriptor](./cinna_mcp_descriptor.md)), and `bundle_version` (installed-vs-latest bundle version state — see [Showing & Applying Bundle Updates](#showing--applying-bundle-updates))
4. Client renders the list with prompt-example chips; tapping an agent opens a new conversation
5. Cinna Desktop uses the `mcp` field to wrap each agent as an emulated MCP tool without re-fetching individual cards; `mcp` is `null` for `identity` targets

### Showing & Applying Bundle Updates

Native clients show "v1.0 → v1.2 update available" on installed agents and let the user update in-app, without the web SPA.

1. The discovery payload (`GET /external/agents`) carries a `bundle_version` object on each `target_type="agent"` target that is one of the caller's own consumer installs (`installed_version`, `installed_revision_number`, `latest_version`, `latest_revision_number`, `update_available`, `update_mode`, `last_update_status`). It is `null` for the publisher's own working copy, plain (never-from-a-bundle) agents, and identity contacts — none of which the caller updates
2. `bundle_version` on the discovery list is computed **read-only**: `update_available` is derived purely from the monotonic `revision_number` comparison, and building it never mutates `Agent.pending_update` (discovery stays write-free)
3. To refresh a single agent on demand, the client calls `POST /external/agents/{agent_id}/check-updates`. Unlike discovery, this delegates to the same `InstallService.check_for_updates` the web surface uses, so it **reconciles** `Agent.pending_update` and returns the canonical `CheckUpdatesResponse`
4. To update, the client calls `POST /external/agents/{agent_id}/apply-update`. This delegates to `InstallService.apply_update` (stops the environment, swaps bundle-owned workspace content, preserves App Data + credentials, restarts) and returns the post-update `bundle_version` snapshot so the client can refresh its UI without a second round-trip
5. Both update endpoints are owner-gated (owner or superuser) — `401` unauthenticated, `403` for a non-owner, `404` for an unknown agent id, `400` only when `InstallService.apply_update` raises `InstallError` (e.g. the install is not linked to a bundle, or the bundle has no published revisions). An already-current install — including the publisher's own working copy — is a silent no-op: `apply_update` clears `pending_update` and returns the install unchanged, and the route returns the current `BundleVersionInfo` snapshot (all-null for a publisher install) without a `400`. They are thin native-surface wrappers over the same services as the web install routes (`POST /agents/{id}/check-updates`, `/apply-update`), so behavior never diverges between web and native clients

### Chatting with a Personal Agent

1. Client fetches or caches the agent card from `GET {agent_card_url}` (or uses `agent_card_url` directly as the JSON-RPC endpoint)
2. Client POSTs `SendStreamingMessage` to `POST /api/v1/external/a2a/agent/{agent_id}/`
3. Backend creates a session with `integration_type="external"` owned by the authenticated user; returns SSE stream
4. Subsequent messages carry the returned `task_id` (which equals the `session_id`) to resume the same thread

### Chatting with an Identity Contact

1. Client POSTs to `POST /api/v1/external/a2a/identity/{owner_id}/`
2. First message (no `task_id`): Stage-2 routing runs against all accessible bindings to pick the agent; new `identity_mcp` session created; `identity_caller_id` set to the requesting user
3. Subsequent messages carry the `task_id` from the first response to resume; binding validity re-checked on every resume
4. Binding disabled mid-conversation → JSON-RPC error `–32004` "This identity connection is no longer active."

### Archiving a Thread

1. Client calls `DELETE /api/v1/external/sessions/{session_id}`
2. Backend sets `session_metadata["hidden_for_callers"] = true` — the session is NOT deleted
3. Session disappears from `GET /external/sessions` for this user; fetching it directly via `GET /external/sessions/{id}` still returns 200
4. Agent owner can still see the session in their own session list

---

## Client Impact: Cinna Desktop and `cinna-cli`

**In scope to report here; out of scope to fix from inside this refactor** (`cinna-cli` is a separate repository at `/Users/evgenyl/dev/ml-llm/cinna-cli`).

- **Removed entirely:** `GET|POST /api/v1/external/a2a/route/{route_id}/` and its `.well-known/agent-card.json` mirror.
- **Removed:** the `"app_mcp_route"` value of `ExternalTargetPublic.target_type` — the type is now `Literal["agent", "identity"]`.
- **`GET /external/agents` shape change:** the response used to carry three ordered sections (personal agents → MCP shared routes → identity contacts); it now carries **two** (personal agents → identity contacts). Any client-side code that indexed or counted sections positionally needs to drop the middle one.
- **Accepted breakage:** a session created through the old `/a2a/route/{route_id}/` path may 404 on a follow-up card fetch, since the route target kind that resolved it no longer exists. This is deliberate (master plan §2.11 — sessions broken by the refactor may be dropped) and is not silently swallowed; the client will see a clear 404, not a wrong answer.
- **Where a Desktop/CLI developer should look:** any code that builds a request against `/a2a/route/...`, or that switches on `target_type === "app_mcp_route"`, needs to be removed. A target previously reached that way is, for its recipient, now reachable only if it is: (a) the caller's own agent (`target_type="agent"`), or (b) offered through an identity binding (`target_type="identity"`) if the two users share one. There is no drop-in replacement for "an admin shared their agent with me via a route" — see [App MCP Server](../app_mcp_server/app_mcp_server.md) for why that avenue is gone rather than migrated (master plan §2.8: routes-as-sharing is replaced by admin-published bundles or by identity, not carried forward).

**One free win this deletion bought:** the card path used to build a target's name from `AppAgentRoute.name` (route target) while discovery built it from `EffectiveRoute.agent_name` (identity target's underlying agent, in the old code) — a documented naming split between two ways of reaching what could be the same agent. With the route path gone, every remaining target's card reads `agent.name` directly. There is one name.

---

## Business Rules

### Discovery (`GET /agents`)
- Returns active agents the user owns (personal), and identity contacts with at least one enabled `IdentityBindingAssignment`
- Each section is sorted by name ascending; sections appear in order: personal → identity
- `?workspace_id=` filters only the personal agents section; the identity section is always fully included
- `a2a_config.enabled` is NOT required on personal agents — the external surface is owner-only and always available

### A2A Endpoints
- `a2a_config.enabled` is NOT checked for `target_type="agent"` (owner has full access regardless)
- Identity binding and assignment validity are re-checked on every message resume — revocation mid-conversation surfaces as `–32004`
- Default protocol is v1.0 (PascalCase method names, `supportedInterfaces` in card); `?protocol=v0.3` switches to slash-case
- `.well-known/agent-card.json` is a mirror of the root card GET endpoint

### Session Visibility
- A session is visible to a user if `user_id == user.id` OR `caller_id == user.id` OR `identity_caller_id == user.id`
- All visibility checks return `404` (not `403`) to avoid leaking session existence to non-participants
- Hidden sessions (`session_metadata["hidden_for_callers"] == true`) are excluded from the listing but remain fetchable by explicit ID

### Session Ownership and `integration_type`
| Target type | `integration_type` | Session owner (`user_id`) | Tracking field |
|-------------|-------------------|--------------------------|----------------|
| `agent` | `"external"` | Requesting user | — |
| `identity` | `"identity_mcp"` | Identity owner | `identity_caller_id = requesting user.id` |

### Client Attribution
- Desktop access tokens include `client_kind="desktop"` and `external_client_id=<DesktopOAuthClient.id>` JWT claims (issued by `DesktopAuthService._create_token_pair`)
- On new session creation, `ExternalA2AContextHandler._stamp_new_session` writes these into `session_metadata` for both integration types if the claims are present
- Non-desktop tokens (web JWTs) carry no such claims; `client_kind` and `external_client_id` remain `null` in `ExternalSessionPublic`
- Native clients can use `client_kind` / `external_client_id` from `ExternalSessionPublic` to filter or label threads by originating device
- These same claims drive the **live revocation check** in `get_current_user`: when a desktop client is disconnected from Settings, the next `/external/...` call with its still-valid access token is rejected with `401 Desktop session has been revoked` (see [Desktop Auth — Live Access Token Revocation Check](../desktop_auth/desktop_auth_tech.md#live-access-token-revocation-check))

---

## Architecture Overview

```
Native Client (Desktop / Mobile)
        │
        │  JWT (standard Cinna auth)
        ▼
GET  /api/v1/external/agents          ExternalAgentCatalogService.list_targets()
                                        ├── personal agents (user.owner_id, optional workspace filter)
                                        └── identity contacts (IdentityService.get_identity_contacts)

POST /api/v1/external/a2a/agent/{id}/     ExternalA2ARequestHandler
POST /api/v1/external/a2a/identity/{id}/   ├── resolves TargetContext (ownership / identity checks)
                                            └── constructs ExternalA2AContextHandler(context=...)
                                                 (subclass of A2ARequestHandler — overrides hooks:
                                                  caller-scope, _stamp_new_session, binding re-check)

GET  /api/v1/external/sessions            ExternalSessionService.list_sessions_for_external()
GET  /api/v1/external/sessions/{id}         (OR-filter: owner | caller | identity_caller; hidden filter)
GET  /api/v1/external/sessions/{id}/messages
DELETE /api/v1/external/sessions/{id}     ExternalSessionService.hide_session_for_external()

POST /api/v1/external/agents/{id}/check-updates   InstallService.check_for_updates() (owner-gated; reconciles pending_update)
POST /api/v1/external/agents/{id}/apply-update     InstallService.apply_update()      (owner-gated; returns BundleVersionInfo)
```

The `bundle_version` snapshot on discovery and the apply-update response are both built by the shared `ExternalAgentCatalogService.build_bundle_version_info(db, agent)`, so the list snapshot and the action response never diverge.

---

## Integration Points

- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the access tokens with `client_kind`/`external_client_id` claims that the external A2A routes extract for client attribution
- **[A2A Protocol](../a2a_integration/a2a_protocol/a2a_protocol.md)** — the underlying JSON-RPC protocol, task/message model, and SSE streaming. `ExternalA2AContextHandler` subclasses `A2ARequestHandler` and overrides its hook methods (`_parse_session_scope`, `_stamp_new_session`, `_task_list_filter`, ...) to enforce caller-scope and stamp metadata — the message-send/stream/task dispatch bodies are shared
- **[App MCP Server](../app_mcp_server/app_mcp_server.md)** — App MCP and this surface are now independent discovery paths over the same underlying agents; there is no `AppAgentRoute` either shares any more. An agent reachable from an MCP client because it has a router trigger prompt is reachable here for the same reason it always was: the caller owns it
- **[Identity MCP Server](../identity_mcp_server/identity_mcp_server.md)** — `IdentityAgentBinding`, `IdentityBindingAssignment`, and Stage-2 routing used for the identity target type; `IdentityRoutingService.route_within_identity` picks the agent on the first message
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — the `Session` model, `session_metadata` JSON column, `integration_type` field, and `caller_id`/`identity_caller_id` fields that the external surface stamps and reads
- **[`cinna.mcp` Descriptor](./cinna_mcp_descriptor.md)** — the `mcp` field on each discovery target and the `urn:cinna:mcp` card extension that let Cinna Desktop wrap agents as emulated MCP tools
- **[Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md)** — supplies the version/update model surfaced here: `Agent.installed_revision_id`, `AgentBundle.latest_revision_id`, and the `InstallService.check_for_updates` / `apply_update` services the native update endpoints delegate to. The `bundle_version` field is the native-client mirror of the web catalog's `latest_version` / `user_install_pending_update` and the install detail page's `UpdateAvailableBanner`

---

*Last updated: 2026-08-25 — Phase 5 of the channels & identity unification refactor removed the `app_mcp_route` target type entirely; discovery is now two sections, not three*
