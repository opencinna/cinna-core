# External Agent Access API

## Purpose

A dedicated REST + A2A surface under `/api/v1/external/` that gives authenticated native clients (Cinna Desktop, future Cinna Mobile) a clean, first-party interface for discovering agents, chatting with them over A2A, and managing their thread history — without the web SPA in the loop.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **External Target** | Any addressable entity the native client can chat with: a personal agent, an MCP shared agent route, or an identity contact |
| **Target Type** | One of `"agent"`, `"app_mcp_route"`, `"identity"` — determines which A2A endpoint family to call |
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
2. Response contains a `targets` list in three ordered sections: personal agents → MCP shared agents → identity contacts
3. Each target includes `name`, `description`, `entrypoint_prompt`, `example_prompts`, `agent_card_url`, and `protocol_versions`
4. Client renders the list with prompt-example chips; tapping an agent opens a new conversation

### Chatting with a Personal Agent

1. Client fetches or caches the agent card from `GET {agent_card_url}` (or uses `agent_card_url` directly as the JSON-RPC endpoint)
2. Client POSTs `SendStreamingMessage` to `POST /api/v1/external/a2a/agent/{agent_id}/`
3. Backend creates a session with `integration_type="external"` owned by the authenticated user; returns SSE stream
4. Subsequent messages carry the returned `task_id` (which equals the `session_id`) to resume the same thread

### Chatting with an MCP Shared Agent Route

1. Client POSTs to `POST /api/v1/external/a2a/route/{route_id}/`
2. Backend re-verifies the caller's assignment is still effective before each request
3. Session is owned by the agent owner; `caller_id` is set to the requesting user; `app_mcp_match_method="external_direct"` is stamped into metadata
4. Revoked assignment → JSON-RPC error `–32004`

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

## Business Rules

### Discovery (`GET /agents`)
- Returns active agents the user owns (personal), agents shared with the user via active `AppAgentRoute` assignments (excluding identity-source routes), and identity contacts with at least one enabled `IdentityBindingAssignment`
- Each section is sorted by name ascending; sections appear in order: personal → shared → identity
- `?workspace_id=` filters only the personal agents section; MCP shared and identity sections are always fully included
- `a2a_config.enabled` is NOT required on personal agents — the external surface is owner-only and always available

### A2A Endpoints
- `a2a_config.enabled` is NOT checked for `target_type="agent"` (owner has full access regardless)
- Route effectiveness is re-verified on every request for `target_type="app_mcp_route"` — a revoked assignment mid-session surfaces as `–32004` on the next message
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
| `app_mcp_route` | `"app_mcp"` | Agent owner | `caller_id = requesting user.id` |
| `identity` | `"identity_mcp"` | Identity owner | `identity_caller_id = requesting user.id` |

### Client Attribution
- Desktop access tokens include `client_kind="desktop"` and `external_client_id=<DesktopOAuthClient.id>` JWT claims (issued by `DesktopAuthService._create_token_pair`)
- On new session creation, `ExternalA2AContextHandler._stamp_new_session` writes these into `session_metadata` for all three integration types if the claims are present
- Non-desktop tokens (web JWTs) carry no such claims; `client_kind` and `external_client_id` remain `null` in `ExternalSessionPublic`
- Native clients can use `client_kind` / `external_client_id` from `ExternalSessionPublic` to filter or label threads by originating device

---

## Architecture Overview

```
Native Client (Desktop / Mobile)
        │
        │  JWT (standard Cinna auth)
        ▼
GET  /api/v1/external/agents          ExternalAgentCatalogService.list_targets()
                                        ├── personal agents (user.owner_id, optional workspace filter)
                                        ├── MCP shared routes (AppAgentRouteService.get_effective_routes_for_user)
                                        └── identity contacts (IdentityService.get_identity_contacts)

POST /api/v1/external/a2a/agent/{id}/
POST /api/v1/external/a2a/route/{id}/    ExternalA2ARequestHandler
POST /api/v1/external/a2a/identity/{id}/   ├── resolves TargetContext (ownership / route / identity checks)
                                            └── constructs ExternalA2AContextHandler(context=...)
                                                 (subclass of A2ARequestHandler — overrides hooks:
                                                  caller-scope, _stamp_new_session, binding re-check)

GET  /api/v1/external/sessions            ExternalSessionService.list_sessions_for_external()
GET  /api/v1/external/sessions/{id}         (OR-filter: owner | caller | identity_caller; hidden filter)
GET  /api/v1/external/sessions/{id}/messages
DELETE /api/v1/external/sessions/{id}     ExternalSessionService.hide_session_for_external()
```

---

## Integration Points

- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the access tokens with `client_kind`/`external_client_id` claims that the external A2A routes extract for client attribution
- **[A2A Protocol](../a2a_integration/a2a_protocol/a2a_protocol.md)** — the underlying JSON-RPC protocol, task/message model, and SSE streaming. `ExternalA2AContextHandler` subclasses `A2ARequestHandler` and overrides its hook methods (`_parse_session_scope`, `_stamp_new_session`, `_task_list_filter`, ...) to enforce caller-scope and stamp metadata — the message-send/stream/task dispatch bodies are shared
- **[App MCP Server](../app_mcp_server/app_mcp_server.md)** — `AppAgentRoute` and `AppAgentRouteAssignment` models used for the shared-route target type; `ExternalA2ARequestHandler` calls `AppAgentRouteService.get_effective_routes_for_user` to re-verify access
- **[Identity MCP Server](../identity_mcp_server/identity_mcp_server.md)** — `IdentityAgentBinding`, `IdentityBindingAssignment`, and Stage-2 routing used for the identity target type; `IdentityRoutingService.route_within_identity` picks the agent on the first message
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — the `Session` model, `session_metadata` JSON column, `integration_type` field, and `caller_id`/`identity_caller_id` fields that the external surface stamps and reads

---

*Last updated: 2026-04-18*
