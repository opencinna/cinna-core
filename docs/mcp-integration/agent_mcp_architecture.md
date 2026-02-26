# Agent MCP Connector — Architecture Overview

## What It Does

Exposes platform agents as **remote MCP servers** so that any MCP-compatible client — Claude Desktop, Claude.ai, Cursor, Windsurf, VS Code Copilot — can connect to an agent and interact with it as a tool provider. The MCP client sends tool calls, the agent processes them via its environment (Docker container running Google ADK / Claude Code), and results stream back over the MCP protocol.

This is the MCP counterpart of our A2A integration. While A2A is agent-to-agent task delegation (JSON-RPC), MCP is tool-use — an AI client calls our agent as a tool. Both share the same underlying infrastructure (SessionService, MessageService, agent environments), but speak different protocols at the API boundary.

### Why MCP

- **Universal client support** — Claude Desktop, Claude.ai, Cursor, and others already support adding remote MCP servers
- **Zero-config for the user** — paste one URL into the MCP client; it auto-discovers OAuth, auto-registers, handles auth
- **OAuth-based auth** — standardized authentication, no manual token management
- **Richer than chat** — MCP supports tools, resources, and prompts (not just messages)

---

## High-Level Architecture

```
┌──────────────────────┐
│  MCP Client          │
│  (Claude Desktop,    │    Streamable HTTP + OAuth 2.1
│   Cursor, etc.)      │◄──────────────────────────────────►┌──────────────────────────────────┐
└──────────────────────┘                                     │  Our Backend (FastAPI)            │
                                                             │                                  │
                                                             │  Shared OAuth AS:                │
                                                             │  /mcp/oauth/...                  │
                                                             │  (our FastAPI routes)            │
                                                             │                                  │
                                                             │  Per-Connector RS:               │
                                                             │  /mcp/{connector_id}/...         │
                                                             │       ┌────────────────────┐     │
                                                             │       │ MCPServerRegistry   │     │
                                                             │       │  ┌──MCPServer(A)──┐ │     │
                                                             │       │  ├──MCPServer(B)──┤ │     │
                                                             │       │  └──MCPServer(C)──┘ │     │
                                                             │       └─────────┬──────────┘     │
                                                             │                 │                │
                                                             │       tools.py → MCPRequestHandler│
                                                             │                 │                │
                                                             │       SessionService /           │
                                                             │       MessageService             │
                                                             │                 │                │
                                                             └─────────────────┼────────────────┘
                                                                               │
                                                                       SSE / HTTP
                                                                               │
                                                             ┌─────────────────┴────────────────┐
                                                             │  Agent Environment               │
                                                             │  (Docker Container)              │
                                                             │  Google ADK / Claude Code        │
                                                             └──────────────────────────────────┘
```

### AS/RS Separation

The architecture separates OAuth into two distinct roles:

| Role | Implementation | URL Space |
|------|---------------|-----------|
| **Authorization Server (AS)** | Our own FastAPI routes | `/mcp/oauth/...` (shared, one set of endpoints for all connectors) |
| **Resource Server (RS)** | One MCPServer instance per connector, using TokenVerifier | `/mcp/{connector_id}/...` (per-connector) |

**Why this split:**
- The MCP SDK binds `issuer_url` and `resource_server_url` per MCPServer instance — a single instance can't serve different connectors
- Each connector needs its own `resource_server_url` (token audience), but all connectors share one OAuth endpoint set
- The SDK auto-generates `/.well-known/oauth-protected-resource` per connector, pointing to the shared AS
- Our FastAPI OAuth routes handle all authorization logic — DCR, authorize, token exchange, revocation

---

## Core Concepts

### MCP Connector

An **MCP Connector** is a named, scoped access point that exposes an agent as an MCP server. It is the MCP equivalent of an A2A access token or a guest share link.

| Property | Description |
|----------|-------------|
| **name** | Display name (e.g., "Cursor - conversation", "Claude Desktop - building") |
| **mode** | `conversation` or `building` — controls which agent mode MCP sessions use |
| **is_active** | Enable/disable without deletion |
| **allowed_emails** | Email addresses allowed to authenticate (empty = owner only) |
| **max_clients** | Maximum DCR client registrations allowed (default 10) |
| **MCP Server URL** | `{MCP_SERVER_BASE_URL}/{connector_id}/mcp` — the only thing the user copies |

**Why multiple connectors?** Different MCP clients may need different permission levels:
- A "conversation" connector for quick tasks (cheaper, faster)
- A "building" connector for development work (full tool access)
- Separate connectors for different users or teams (each with its own email list and session isolation)

### Email-Based Access Control

Access to a connector is controlled by an `allowed_emails` list, not by URL secrecy:

- **Owner always has access** — bypasses the email check
- **Empty list = owner only** — default, most restrictive
- **Explicit sharing** — owner adds email addresses to grant access
- **Checked during OAuth consent** — unauthorized users cannot obtain tokens even if they have the URL
- **Revocable without URL change** — remove an email to revoke access; no need to re-create the connector

This is more secure than URL-as-security-boundary because URLs can leak inadvertently (browser history, chat logs, screenshots), while the email list gives explicit, auditable control.

### Mapping MCP Primitives to the Platform

| MCP Primitive | Platform Mapping |
|---------------|-----------------|
| **Tool** (`send_message`) | Send a message to the agent, receive a JSON response with `response` and `context_id` |
| **`context_id`** | Platform Session UUID — echoed by LLM to maintain per-chat continuity |
| **MCP Session** | Transport-level session (shared across chats); stored as metadata, not used for routing |
| **Resource** (future) | Agent workspace files |
| **Prompt** (future) | Agent prompt templates |

### Tool Design

The agent exposes a single primary tool:

**`send_message(message, context_id)`** — Send a message to the agent and receive a response. Returns a JSON object with `response` and `context_id` fields.

**Per-chat session isolation via `context_id`:**
- On the first call in a new chat, `context_id` is empty → a new platform session is created → the response includes the session's `context_id` (its UUID)
- On subsequent calls, the LLM passes back the `context_id` from the previous response → the same session is reused
- A new chat = fresh LLM context = no `context_id` = new session

This solves a fundamental problem: Claude Desktop (and similar MCP clients) reuse a single MCP transport session (`mcp-session-id`) across all chats. Without `context_id`, all chats would land in the same platform session. The `context_id` parameter lets us distinguish chats even when the transport session is shared.

### Service Layer Architecture

The tool handler follows the same isolation pattern as `A2ARequestHandler`:

```
tools.py (thin MCP entry point)
    ├─ Extract MCP context vars (connector_id, mcp_session_id)
    ├─ MCPConnectorService.resolve_connector_context() → (connector, agent, environment)
    └─ MCPRequestHandler.handle_send_message(message, mcp_session_id, context_id)
            ├─ SessionService.get_or_create_mcp_session(context_id=...)
            ├─ MessageService.create_message()
            ├─ SessionService.ensure_environment_ready_for_streaming()
            ├─ MessageService.stream_message_with_events()
            └─ Return JSON: {"response": "...", "context_id": "<session_uuid>"}
```

**Key principle:** No direct database queries in `MCPRequestHandler` — all data access goes through `SessionService` and `MessageService`, matching the A2A handler pattern.

---

## Authentication & Authorization Flow

### End-to-End Flow

```
User creates MCP Connector in our UI → copies MCP Server URL
    │
    ▼

MCP Client                  Per-Connector RS                 Shared OAuth AS
    │                        /{connector_id}/                /oauth/
    │                           │                                │
    │──POST /mcp (no token)────►│                                │
    │◄──401 + WWW-Authenticate──│                                │
    │                           │                                │
    │──GET /.well-known/────────►│                                │
    │  oauth-protected-resource │                                │
    │◄──{authorization_servers}─│                                │
    │                           │                                │
    │──GET .well-known/oauth-authorization-server────────────────►│
    │◄──{authorize, token, registration_endpoint}─────────────────│
    │                           │                                │
    │──POST /register (+ resource param)─────────────────────────►│
    │◄──{client_id, client_secret}────────────────────────────────│
    │                           │                                │
    │  [Browser: /authorize + client_id + PKCE + resource]───────►│
    │  [Backend checks email ACL, redirects to consent page]      │
    │  [User logs in + approves on consent screen]                │
    │◄─[Redirect with auth code]──────────────────────────────────│
    │                           │                                │
    │──POST /token + PKCE verifier───────────────────────────────►│
    │◄──{access_token, refresh_token}─────────────────────────────│
    │                           │                                │
    │──POST /mcp (Bearer token)►│                                │
    │  [TokenVerifier validates] │                                │
    │◄──MCP response────────────│                                │
```

The MCP client handles the entire flow automatically — the user only pastes the URL.

### OAuth Endpoints (Shared AS)

| Endpoint | Purpose | Standard |
|----------|---------|----------|
| `GET /mcp/oauth/.well-known/oauth-authorization-server` | AS metadata — clients discover all endpoints | RFC 8414 |
| `POST /mcp/oauth/register` | Dynamic Client Registration — MCP clients auto-register | RFC 7591 |
| `GET /mcp/oauth/authorize` | Authorization — stores request, redirects to frontend consent | OAuth 2.1 |
| `POST /mcp/oauth/token` | Token exchange — auth code → tokens, refresh → new token | OAuth 2.1 |
| `POST /mcp/oauth/revoke` | Token revocation — immediate invalidation | RFC 7009 |

### Per-Connector Endpoints (RS, SDK-managed)

| Endpoint | Purpose |
|----------|---------|
| `GET /mcp/{connector_id}/.well-known/oauth-protected-resource` | Points MCP client to the shared AS (auto-generated by SDK) |
| `POST /mcp/{connector_id}/mcp` | MCP Streamable HTTP — tool calls, JSON-RPC |
| `GET /mcp/{connector_id}/mcp` | SSE stream for server-initiated notifications |
| `DELETE /mcp/{connector_id}/mcp` | Terminate MCP session |

### Token Model

Tokens are **opaque strings** (not JWTs) stored in the database:

| Token Type | Expiry | Purpose |
|------------|--------|---------|
| Access token | 1 hour | Bearer token on MCP requests |
| Refresh token | 30 days | Exchange for new access token |

Database-backed tokens enable immediate revocation (no waiting for JWT expiry) and are validated via lookup on each MCP request. The TokenVerifier additionally checks:
- Token not expired or revoked
- Token's connector matches the connector being accessed (cross-connector isolation)
- Connector is still active

### Consent Flow

When the OAuth `/authorize` endpoint is hit:

1. Backend stores the full OAuth request server-side (keyed by random nonce) — prevents parameter tampering
2. Redirects to frontend: `/oauth/mcp-consent?nonce={nonce}`
3. Frontend fetches consent details via API — shows agent name, connector mode, client name, scopes
4. User logs in (if needed) and approves
5. Backend checks email ACL (owner or in `allowed_emails`) and issues auth code
6. Browser redirects back to MCP client with auth code
7. MCP client exchanges code for tokens

---

## Session & Message Flow

### MCP ↔ Platform Session Mapping

```
MCP Client connects (initialize → Mcp-Session-Id assigned by SDK)
    │
    ├─ tools/list → Returns [send_message(message, context_id)]
    │
    ├─ Chat 1: tools/call "send_message" {message: "...", context_id: ""}    ← first call
    │   │
    │   ├─ tools.py: extract context vars, resolve entities
    │   ├─ MCPRequestHandler.handle_send_message(context_id=""):
    │   │   ├─ SessionService.get_or_create_mcp_session(context_id=None) → create new
    │   │   ├─ MessageService.create_message() (user message)
    │   │   ├─ Stream response from agent environment
    │   │   └─ Return JSON: {"response": "...", "context_id": "<session-uuid-A>"}
    │   └─ LLM receives context_id, stores in its context window
    │
    ├─ Chat 1: tools/call "send_message" {message: "...", context_id: "<session-uuid-A>"}
    │   │                                                                ← LLM echoes it back
    │   ├─ MCPRequestHandler: look up session by context_id → found → reuse
    │   └─ Same session, conversation continues
    │
    ├─ Chat 2: tools/call "send_message" {message: "...", context_id: ""}
    │   │                                     ← new chat = fresh LLM context = no context_id
    │   ├─ MCPRequestHandler: no context_id → create new session
    │   └─ Return JSON: {"response": "...", "context_id": "<session-uuid-B>"}
    │
    └─ Chat 1 and Chat 2 are fully isolated despite sharing the same MCP transport session
```

**Key behaviors:**
- Session lookup is driven exclusively by `context_id` (the platform session UUID)
- `mcp_session_id` (MCP transport session) is stored as metadata but NOT used for session routing — Claude Desktop reuses the same transport session across all chats, so it cannot distinguish them
- `context_id` is cross-connector verified: a context_id from connector A is rejected on connector B
- Invalid/garbage `context_id` values gracefully create a new session
- Session belongs to the connector owner (like guest sessions)
- Agent environment auto-starts if suspended/stopped (via `ensure_environment_ready_for_streaming`)
- Sequential message processing with per-session locking (concurrent calls rejected)
- `mcp_token.user_id` tracks who authenticated (audit trail)
- Same service layer isolation as A2A: `MCPRequestHandler` delegates to `SessionService`/`MessageService` (no direct DB queries)

---

## URL Scheme & Configuration

### Routing Table

| URL Pattern | Handler | Purpose |
|-------------|---------|---------|
| `/mcp/oauth/...` | Our FastAPI routes | Shared OAuth AS (metadata, DCR, authorize, token, revoke) |
| `/mcp/{connector_id}/.well-known/...` | SDK auto-generated | Protected Resource Metadata |
| `/mcp/{connector_id}/mcp` | SDK MCPServer (per-connector) | MCP Streamable HTTP endpoint |
| `/api/v1/agents/{agent_id}/mcp-connectors/...` | Our FastAPI routes | Connector CRUD (frontend management) |
| `/api/v1/mcp/consent/{nonce}` | Our FastAPI routes | Consent page API |

**Important:** The `/mcp/` routes are for external MCP clients only. The frontend manages connectors via `/api/v1/` endpoints.

### `MCP_SERVER_BASE_URL` Configuration

All externally-facing MCP URLs are derived from one config variable:

| What | URL |
|------|-----|
| MCP Server URL (user copies this) | `{MCP_SERVER_BASE_URL}/{connector_id}/mcp` |
| AS Metadata | `{MCP_SERVER_BASE_URL}/oauth/.well-known/oauth-authorization-server` |
| OAuth authorize / token / register | `{MCP_SERVER_BASE_URL}/oauth/...` |
| Token audience (resource param) | `{MCP_SERVER_BASE_URL}/{connector_id}/mcp` |

**Production (dedicated subdomain):**
```
MCP_SERVER_BASE_URL=https://mcp.domain.com
```
nginx proxies `mcp.domain.com` → `backend:8000/mcp/`

**Local development (tunnel):**
```
MCP_SERVER_BASE_URL=https://abc123.a.free.pinggy.link/mcp
```
Tunnel forwards to `localhost:8000/mcp/`

---

## Frontend

### Connector Management (Integrations Tab)

The **MCPConnectorsCard** lives in the agent's Integrations tab alongside A2A, Access Tokens, Email, and Guest Share cards.

**Features:**
- Create connector: name, mode (conversation/building), allowed emails
- List connectors with status badges (active/inactive), mode badges
- Copyable MCP Server URL (the one thing users paste into their MCP client)
- Allowed emails display as badges
- Toggle active/inactive, delete with confirmation

### OAuth Consent Page

Public route at `/oauth/mcp-consent` (outside normal auth guard):
- Displays: agent name, connector name/mode, client name, requested scopes
- Redirects to login if unauthenticated
- Authorize / Deny buttons
- On approval → redirects to MCP client's callback with auth code

---

## How It Fits With Other Integrations

| Aspect | A2A | MCP | Guest Sessions |
|--------|-----|-----|----------------|
| **Protocol** | A2A JSON-RPC | MCP JSON-RPC (standard) | WebSocket + REST |
| **Auth** | Bearer JWT (custom tokens) | OAuth 2.1 + DCR (zero-config) | Share links |
| **Client support** | Custom A2A clients | Claude Desktop, Cursor, etc. | Browser |
| **Interaction** | Task-based | Tool-based | Chat-based |
| **Access control** | Access tokens (mode + scope) | Email-based ACL | Share links with optional password |
| **Session ownership** | Access token owner | Connector owner | Guest or share creator |
| **Use case** | Agent-to-agent delegation | AI tool calling our agent | Human chat with agent |

All three share the same underlying services: SessionService, MessageService, agent environments.

---

## Security Model

### Authentication Layers

1. **Email ACL** — Only connector owner or users in `allowed_emails` can complete OAuth
2. **PKCE** — Required for all clients; prevents authorization code interception
3. **Client credentials** — DCR-issued `client_id` + `client_secret` (secret stored as SHA256 hash)
4. **Opaque tokens** — Database-backed, immediately revocable
5. **Token audience validation** — TokenVerifier ensures tokens are connector-scoped (cross-connector isolation)
6. **Connector active check** — Deactivated connectors reject all requests

### Cascade Behavior

- **Connector deleted** → All OAuth clients, auth codes, tokens CASCADE-deleted
- **Connector deactivated** → MCP server evicted from registry; TokenVerifier rejects all tokens
- **Email removed from ACL** → Existing tokens remain valid until expiry; for immediate revocation, deactivate the connector

### Session Isolation

- Session routing is driven by `context_id`, not by `mcp_session_id` (transport session)
- `context_id` is cross-connector verified: session must belong to the requesting connector
- Each new chat (empty `context_id`) creates a new platform session
- No cross-session visibility between chats
- Mode enforcement: connector mode determines session mode

---

## Design Decisions

1. **Fixed tool set** — `send_message` rather than auto-generating tools from agent config. Keeps the MCP surface predictable. The agent decides which capabilities to invoke based on the message.

2. **`context_id` for per-chat isolation** — Claude Desktop (and similar clients) reuse a single MCP transport session across all chats, so `mcp_session_id` cannot distinguish conversations. Instead, `send_message` returns a `context_id` (the platform session UUID) that the LLM echoes back on subsequent calls. New chat = fresh LLM context = empty `context_id` = new session. The `mcp_session_id` is stored as metadata but never used for session lookup.

3. **Opaque tokens, not JWTs** — Database lookup on each request enables immediate revocation. We already hit the DB for connector validation, so token lookup adds minimal overhead.

4. **One MCPServer per connector** — Required because the SDK binds `resource_server_url` per instance. Also provides natural isolation and simplifies TokenVerifier.

5. **Shared OAuth AS** — Single set of OAuth endpoints for all connectors, using `resource` parameter (RFC 8707) to identify which connector. Avoids duplicating OAuth logic per connector.

6. **Email-based ACL over URL secrecy** — URLs leak easily. Email list gives explicit, auditable, revocable access control without changing the connector URL.

7. **Server-side OAuth request storage** — The consent page receives only an opaque nonce. All sensitive OAuth params stored server-side, preventing parameter tampering by the frontend.

8. **Sequential message processing** — Concurrent `send_message` calls on the same session are queued. Prevents race conditions in agent environment communication.

---

## Future Phases

### Phase 2: Enhanced Tools & Resources
- Workspace resources: `resources/list` and `resources/read` for agent workspace files
- Session management tools: `list_sessions`, `get_session_status`
- Interrupt support, file upload

### Phase 3: Advanced Features
- Prompt templates via MCP `prompts/list`
- External OAuth providers (Auth0/Keycloak) — swap AS, keep TokenVerifier
- Per-connector rate limiting and usage tracking

---

## Protocols & Standards

| Standard | RFC/Spec | Role |
|----------|----------|------|
| MCP Streamable HTTP | [MCP Spec 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http) | Transport layer |
| MCP Authorization | [MCP Spec 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | OAuth flow requirements |
| OAuth 2.1 | [draft-ietf-oauth-v2-1-13](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13) | Core auth framework |
| PKCE | OAuth 2.1 §7.5.2 | Code exchange protection |
| Protected Resource Metadata | [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728) | Server → AS discovery |
| Auth Server Metadata | [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414) | Client → endpoint discovery |
| Dynamic Client Registration | [RFC 7591](https://datatracker.ietf.org/doc/html/rfc7591) | Auto-registration |
| Resource Indicators | [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707.html) | Token audience binding |
| Token Revocation | [RFC 7009](https://datatracker.ietf.org/doc/html/rfc7009) | Token invalidation |

---

## Related Documentation

- `docs/agent_mcp_connector_concept.md` — Original concept document (detailed SDK analysis, resolved design questions)
- `docs/mcp-integration/implementation_plan.md` — Phased implementation plan
- `docs/mcp-integration/agent_mcp_connector.md` — Code-level implementation reference
- `docs/a2a/a2a_integration.md` — A2A integration (comparable feature)

---

**Document Version:** 1.2
**Last Updated:** 2026-02-26
**Status:** Implemented (Phase 1 MVP, per-chat session isolation via context_id)
