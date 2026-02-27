# Agent MCP Connector - Implementation Reference

## Purpose

Enable agent owners to expose their agents as MCP (Model Context Protocol) servers, allowing external MCP clients (Claude Desktop, Cursor) to connect, authenticate via OAuth 2.1, and interact with the agent through a `send_message` tool.

## Feature Overview

- Agent owner creates named MCP connectors with mode (conversation/building) and email ACL
- Each connector gets a unique MCP server URL: `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`
- External MCP clients discover the shared OAuth Authorization Server via standard metadata endpoints
- Dynamic Client Registration (RFC 7591) allows MCP clients to register automatically
- OAuth 2.1 authorization flow with PKCE and frontend consent page
- Authenticated MCP clients call `send_message` tool to interact with the agent
- Per-chat session isolation via `context_id` parameter (LLM echoes it back for continuity)
- `send_message` returns JSON with `response` and `context_id` fields
- Sequential message processing with per-session locking

## Architecture

```
MCP Client (Claude Desktop / Cursor)
        │
        ├─ Discovery: GET /.well-known/oauth-authorization-server
        ├─ DCR: POST /mcp/oauth/register
        ├─ Authorize: GET /mcp/oauth/authorize → redirect to consent page
        ├─ Token: POST /mcp/oauth/token
        │
        └─ Tool call: POST /mcp/{connector_id}/mcp (send_message)
                │
                ▼
        MCPServerRegistry (ASGI dispatcher)
                │
                ├─ Extract connector_id from path
                ├─ Lazy-create per-connector FastMCP server
                ├─ MCPTokenVerifier validates bearer token
                │
                └─ send_message tool handler
                        │
                        ├─ Get/create platform session (linked to connector)
                        └─ Stream response from agent environment
```

**Key Concepts:**
- **MCP Connector**: A named configuration linking an agent to an MCP server endpoint
- **Shared OAuth AS**: Single authorization server at `/mcp/oauth/` serving all connectors
- **MCPServerRegistry**: ASGI dispatcher that routes requests to per-connector FastMCP instances
- **MCPTokenVerifier**: Validates bearer tokens per-connector (checks expiry, revocation, connector match)

## Data/State Lifecycle

### Connector Modes

| Mode | Description |
|------|-------------|
| `conversation` | Chat interactions with the agent |
| `building` | Development/builder tasks |

### Email Access Control

| Configuration | Who Can Authorize |
|---------------|-------------------|
| `allowed_emails = []` | Owner only |
| `allowed_emails = ["a@b.com"]` | Owner + listed emails |

### OAuth Token Lifecycle

| Token Type | Expiry | Storage |
|------------|--------|---------|
| Access token | 1 hour | `mcp_token` table, opaque string |
| Refresh token | 30 days | `mcp_token` table, opaque string |
| Auth code | 5 minutes | `mcp_auth_code` table |
| Auth request (nonce) | 10 minutes | `mcp_auth_request` table |

## Database Schema

### Migration

`backend/app/alembic/versions/dc259404533e_add_mcp_integration_tables.py`

### Table: `mcp_connector`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID, PK | Connector identifier |
| `agent_id` | UUID, FK → agent.id (CASCADE) | Linked agent |
| `owner_id` | UUID, FK → user.id (CASCADE) | Connector owner |
| `name` | VARCHAR(255) | Display name |
| `mode` | VARCHAR | "conversation" or "building" |
| `is_active` | BOOLEAN | Whether connector accepts connections |
| `allowed_emails` | JSON | Email ACL list (empty = owner only) |
| `max_clients` | INTEGER | Maximum DCR registrations (default 10) |
| `created_at`, `updated_at` | DATETIME | Timestamps |

**Models:** `backend/app/models/mcp_connector.py`
- `MCPConnector` (table model)
- `MCPConnectorCreate`, `MCPConnectorUpdate` (input schemas)
- `MCPConnectorPublic`, `MCPConnectorsPublic` (response schemas)
- `MCPConnectorPublic` includes computed `mcp_server_url` field
- `MCPConnectorsPublic` includes `mcp_server_base_url` (current env value, used by frontend to construct URLs dynamically)

### Table: `mcp_oauth_client`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID, PK | Internal ID |
| `client_id` | VARCHAR, unique, indexed | OAuth client identifier |
| `client_secret_hash` | VARCHAR | SHA256 hash of client secret |
| `client_name` | VARCHAR | Client display name |
| `redirect_uris` | JSON | Allowed redirect URIs |
| `grant_types` | JSON | Supported grants (default: authorization_code, refresh_token) |
| `response_types` | JSON | Supported responses (default: code) |
| `connector_id` | UUID, FK → mcp_connector.id (CASCADE) | Parent connector |
| `created_at` | DATETIME | Registration timestamp |

**Models:** `backend/app/models/mcp_oauth_client.py`
- `MCPOAuthClient` (table model)
- `MCPOAuthClientPublic` (response schema)

### Table: `mcp_auth_code`

| Column | Type | Description |
|--------|------|-------------|
| `code` | VARCHAR, PK | Authorization code (token_urlsafe 48) |
| `client_id` | VARCHAR, indexed | OAuth client |
| `user_id` | UUID, FK → user.id (CASCADE) | Authorizing user |
| `connector_id` | UUID, FK → mcp_connector.id (CASCADE) | Target connector |
| `redirect_uri` | VARCHAR | Client callback URL |
| `code_challenge` | VARCHAR | PKCE challenge |
| `scope` | VARCHAR | Requested scopes |
| `resource` | VARCHAR | MCP resource URL |
| `expires_at` | DATETIME | Code expiry (5 min) |
| `used` | BOOLEAN | Whether code has been exchanged |
| `created_at` | DATETIME | Timestamp |

### Table: `mcp_auth_request`

| Column | Type | Description |
|--------|------|-------------|
| `nonce` | VARCHAR, PK | Random nonce for consent page lookup |
| `connector_id` | UUID, FK → mcp_connector.id (CASCADE) | Target connector |
| `client_id` | VARCHAR | OAuth client |
| `redirect_uri` | VARCHAR | Client callback URL |
| `code_challenge` | VARCHAR | PKCE challenge |
| `code_challenge_method` | VARCHAR | "S256" |
| `scope` | VARCHAR | Requested scopes |
| `state` | VARCHAR | OAuth state parameter |
| `resource` | VARCHAR | MCP resource URL |
| `expires_at` | DATETIME | Request expiry (10 min) |
| `used` | BOOLEAN | Whether request was approved |
| `created_at` | DATETIME | Timestamp |

**Models:** `backend/app/models/mcp_auth_code.py`
- `MCPAuthCode` (table model)
- `MCPAuthRequest` (table model)

### Table: `mcp_token`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID, PK | Internal ID |
| `token` | VARCHAR, unique, indexed | Opaque bearer token (token_urlsafe 48) |
| `token_type` | VARCHAR | "access" or "refresh" |
| `client_id` | VARCHAR, indexed | OAuth client |
| `user_id` | UUID, FK → user.id (CASCADE) | Token owner |
| `connector_id` | UUID, FK → mcp_connector.id (CASCADE), indexed | Target connector |
| `scope` | VARCHAR | Granted scopes |
| `resource` | VARCHAR | MCP resource URL |
| `expires_at` | DATETIME | Token expiry |
| `revoked` | BOOLEAN | Revocation flag |
| `created_at` | DATETIME | Timestamp |

**Models:** `backend/app/models/mcp_token.py`
- `MCPToken` (table model)

### Session Table Updates

**File:** `backend/app/models/session.py:46-50`

Two new nullable fields added to the `session` table:
- `mcp_connector_id` (UUID, FK → mcp_connector.id, SET NULL on delete)
- `mcp_session_id` (VARCHAR, unique, nullable) — MCP transport session ID, stored as metadata only (NOT used for session lookup; `context_id` drives session routing instead)

## Backend Implementation

### API Routes — Connector CRUD

**File:** `backend/app/api/routes/mcp_connectors.py`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/agents/{agent_id}/mcp-connectors` | Create connector |
| `GET` | `/api/v1/agents/{agent_id}/mcp-connectors` | List connectors for agent |
| `GET` | `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}` | Get connector detail |
| `PUT` | `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}` | Update connector |
| `DELETE` | `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}` | Delete connector |

**Router Registration:** `backend/app/api/main.py` (tag: `mcp-connectors`)

**Ownership Enforcement:** All operations verify the current user owns the agent via `_check_agent_owner()`.

### API Routes — OAuth Consent

**File:** `backend/app/api/routes/mcp_consent.py`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/mcp/consent/{nonce}` | Fetch auth request details (public, no auth) |
| `POST` | `/api/v1/mcp/consent/{nonce}/approve` | Approve consent (requires JWT auth) |

**Router Registration:** `backend/app/api/main.py` (tag: `mcp-consent`)

**Consent Info Response (`ConsentInfo`):** agent name, connector name/mode, client name, scopes, expiry.

**Approval Logic:**
1. Validate nonce exists, not used, not expired
2. Check email ACL: user is connector owner OR email in `allowed_emails`
3. Mark auth request as used
4. Generate auth code (5 min expiry)
5. Return redirect URL with code + state params

### OAuth Authorization Server

**File:** `backend/app/mcp/oauth_routes.py`

Mounted at `/mcp/oauth` in `backend/app/main.py`.

| Endpoint | Purpose | Spec |
|----------|---------|------|
| `GET /.well-known/oauth-authorization-server` | AS metadata | RFC 8414 |
| `POST /register` | Dynamic Client Registration | RFC 7591 |
| `GET /authorize` | OAuth authorize (redirects to consent) | OAuth 2.1 |
| `POST /token` | Token exchange (auth code + refresh) | OAuth 2.1 |
| `POST /revoke` | Token revocation | RFC 7009 |

**AS Metadata Response:**
```json
{
  "issuer": "{MCP_SERVER_BASE_URL}/oauth",
  "authorization_endpoint": "{MCP_SERVER_BASE_URL}/oauth/authorize",
  "token_endpoint": "{MCP_SERVER_BASE_URL}/oauth/token",
  "registration_endpoint": "{MCP_SERVER_BASE_URL}/oauth/register",
  "revocation_endpoint": "{MCP_SERVER_BASE_URL}/oauth/revoke",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "token_endpoint_auth_methods_supported": ["client_secret_post"],
  "code_challenge_methods_supported": ["S256"],
  "scopes_supported": ["mcp:tools", "mcp:resources"]
}
```

**DCR (`/register`):**
- Extracts `connector_id` from `resource` URL via `_extract_connector_id_from_resource()`
- Validates connector exists and is active
- Enforces `max_clients` limit (429 if exceeded)
- Generates `client_id` (UUID) + `client_secret` (48 bytes, base64url)
- Stores `client_secret` as SHA256 hash

**`/authorize`:**
- Validates `client_id` belongs to connector (from `resource`)
- Stores full request in `mcp_auth_request` (keyed by nonce, 10 min expiry)
- Redirects to `{FRONTEND_HOST}/oauth/mcp-consent?nonce={nonce}`

**`/token` — Authorization Code Grant:**
- Validates client credentials (client_secret_post)
- Looks up auth code, verifies not used/expired
- Verifies PKCE S256 (`code_verifier` vs `code_challenge`)
- Verifies `resource` matches auth code
- Generates access token (1hr) + refresh token (30d)

**`/token` — Refresh Token Grant:**
- Validates client credentials
- Verifies refresh token not revoked/expired
- Generates new access token (1hr)

**`/revoke`:**
- Marks token as `revoked=True`
- Always returns 200 per RFC 7009

### Connector Service

**File:** `backend/app/services/mcp_connector_service.py`

**Class:** `MCPConnectorService` (static methods)

**Methods:**
- `create_connector(db_session, agent_id, owner_id, data)` — Create connector
- `list_connectors(db_session, agent_id, owner_id)` — List connectors for agent/owner
- `get_connector(db_session, connector_id)` — Get by ID
- `update_connector(db_session, connector_id, owner_id, data)` — Update with ownership check; evicts MCP server if deactivated
- `delete_connector(db_session, connector_id, owner_id)` — Delete with ownership check; evicts MCP server from registry
- `resolve_connector_context(db_session, connector_id)` — Load and validate connector, agent, environment for tool requests (used by tools.py entry point)
- `check_email_access(db_session, connector_id, email)` — Check email in `allowed_emails`
- `get_registered_client_count(db_session, connector_id)` — Count registered OAuth clients
- `to_public(connector)` — Convert to public dict with computed `mcp_server_url`

### MCP Server Infrastructure

#### Token Verifier

**File:** `backend/app/mcp/token_verifier.py`

**Class:** `MCPTokenVerifier(TokenVerifier)` — implements MCP SDK `TokenVerifier` protocol

**`verify_token(token)` logic:**
1. Look up token in `mcp_token` table (type=access)
2. Check expiry
3. Check revocation
4. Verify `connector_id` matches this verifier's connector
5. Verify connector is still active
6. Return SDK `AccessToken` or `None`

#### Server Factory

**File:** `backend/app/mcp/server.py`

**`create_mcp_server_for_connector(connector_id)`:**
- Creates `FastMCP` instance with `AuthSettings` pointing to shared OAuth AS
- Attaches `MCPTokenVerifier` for the connector
- Registers `send_message` and `get_file_upload_url` tools via `register_mcp_tools()`
- Registers workspace resources via `register_mcp_resources()`
- Registers agent example prompts via `register_mcp_prompts()`

#### MCPServerRegistry

**File:** `backend/app/mcp/server.py`

**Class:** `MCPServerRegistry` — ASGI dispatcher mounted at `/mcp`

**Instance:** `mcp_registry` (singleton)

**Behavior:**
- `get_or_create(connector_id)` — Lazy-creates per-connector FastMCP ASGI apps; validates connector exists and is active
- `remove(connector_id)` — Evicts cached server (called on deactivation/deletion)
- `clear()` — Clears all servers (called on app shutdown)
- `__call__(scope, receive, send)` — ASGI dispatcher: extracts `connector_id` from path, sets `mcp_connector_id_var` contextvar, delegates to per-connector app

**Context Variable:** `mcp_connector_id_var: ContextVar[str]` — set by registry, read by tool handlers

#### Mounting in FastAPI

**File:** `backend/app/main.py:208-213`

```python
# MCP OAuth routes (must be before any /mcp mount)
app.include_router(mcp_oauth_router, prefix="/mcp/oauth")

# Per-connector MCP server mount (must be after /mcp/oauth routes)
app.mount("/mcp", mcp_registry)
```

Starlette routing precedence ensures `/mcp/oauth/...` routes match before the `/mcp/...` mount.

**Lifespan:** `mcp_registry.clear()` called on shutdown.

### Tool Handlers & Request Handler

**Architecture:** Follows the same isolation pattern as `A2ARequestHandler`:
- `tools.py` is a thin MCP-specific entry point (analogous to A2A route)
- `MCPRequestHandler` handles business logic through service layer (analogous to A2ARequestHandler)
- No direct database queries in the request handler — all data access through `SessionService` and `MessageService`

**File:** `backend/app/mcp/tools.py` (MCP-specific entry point)

`handle_send_message(message, context_id, ctx)` logic:
1. Extract `connector_id` from `mcp_connector_id_var` contextvar
2. Extract MCP transport session ID from contextvar and/or tool context
3. Resolve connector, agent, environment via `MCPConnectorService.resolve_connector_context()`
4. Create `MCPRequestHandler` with resolved entities
5. Delegate to `handler.handle_send_message(message, mcp_session_id, context_id)`

**File:** `backend/app/mcp/request_handler.py` (business logic handler)

**Class:** `MCPRequestHandler`

`handle_send_message(message, mcp_session_id, context_id)` logic:
1. Get or create platform session via `SessionService.get_or_create_mcp_session(context_id=...)`
   - If `context_id` provided → look up session by UUID, verify it belongs to this connector
   - If not provided or invalid → create new session
   - `mcp_session_id` is stored as metadata on new sessions but NOT used for session lookup
2. Create user message via `MessageService.create_message()`
3. Trigger title generation for new sessions via `SessionService.auto_generate_session_title()`
4. Ensure environment is ready via `SessionService.ensure_environment_ready_for_streaming()`
5. Acquire per-session `asyncio.Lock` for sequential processing
6. Stream response via `MessageService.stream_message_with_events()` (resolves environment URL/auth internally from `environment_id`)
7. Collect response parts and return JSON: `{"response": "...", "context_id": "<session_uuid>"}`
   - Error responses also JSON: `{"error": "...", "context_id": "..."}`

**Service Layer:** `backend/app/services/mcp_connector_service.py`
- `MCPConnectorService.resolve_connector_context()` — Loads and validates connector, agent, environment for tool requests

**Message Queuing:**
- Per-session locks via `_session_locks` dict in request handler
- Returns error if lock is already held (another message in progress)

**`register_mcp_tools(server)`:** Registers `send_message(message, context_id)` and `get_file_upload_url(filename, workspace_path)` on the FastMCP instance via `@server.tool()`. The tool description instructs the LLM to always pass back the `context_id` from the previous response to maintain conversation continuity. After `send_message` completes, the wrapper sends a `notifications/resources/list_changed` notification to the client (the agent may have created or modified workspace files) and registers the session in the registry for broadcast reuse by the upload route. Notification failures are non-fatal.

### Workspace Resources

**File:** `backend/app/mcp/resources.py`

Exposes agent workspace files as MCP resources so clients can browse and read them via `resources/list`, `resources/templates/list`, and `resources/read`.

**URI scheme:** `workspace://{folder}/{path}` — e.g., `workspace://files/report.csv`, `workspace://scripts/run.sh`

**Security:** Only `files`, `uploads`, `scripts` folders are exposed. Sensitive folders (`credentials/`, `databases/`, `docs/`, `knowledge/`, `logs/`) are excluded. Max resource size: 10MB.

**`register_mcp_resources(server)`:**
1. Replaces `server._resource_manager` with `WorkspaceResourceManager`
2. Re-registers the `list_resources` handler on the low-level MCP server to use the async `list_resources_async()` method (dynamically enumerates workspace files)
3. Registers 3 folder templates for `resources/templates/list` discovery

**`WorkspaceResourceManager(ResourceManager)`:** Custom subclass with two key overrides:
- `list_resources_async()` — Dynamically fetches workspace tree from the agent environment, enumerates all files in allowed folders, and returns them as concrete `FunctionResource` instances. This ensures Claude Desktop (which only reads `resources/list`, not templates) can see every file. Gracefully returns empty if the environment is not running.
- `get_resource()` — Intercepts `workspace://` URIs and parses multi-segment paths manually. The default SDK template matching uses `[^/]+` regex per `{param}`, which doesn't match nested paths like `scripts/sub/folder/run.sh`.

**Helper functions:**
- `_get_adapter_for_connector()` — Resolves context var → connector → agent → environment → adapter (same pattern as `tools.py`)
- `_collect_files_from_tree(tree)` — Walks workspace tree dict, extracts `(path, name, size)` tuples from allowed folders
- `_read_workspace_file(path)` — Calls `adapter.download_workspace_item()`, collects chunks (enforcing size limit), returns `str` for text or `bytes` for binary
- `_parse_workspace_uri(uri)` — Parses `workspace://folder/path` into `(folder, path)` tuple with validation
- `_guess_mime_type(path)` — MIME type detection via `mimetypes.guess_type()`

**Content handling:**
- Text files (MIME type `text/*`, `application/json`, `application/xml`, etc.) → returned as `str` → MCP `TextResourceContents`
- Binary files (everything else) → returned as `bytes` → MCP `BlobResourceContents` (SDK handles base64)

### Agent Example Prompts

**File:** `backend/app/mcp/prompts.py`

Exposes agent-defined example prompts as MCP prompts so clients can discover them via `prompts/list` and `prompts/get`.

**Agent model field:** `example_prompts: list[str]` on `Agent` (JSON column, `backend/app/models/agent.py`). Each line follows `slug: prompt text` format.

**`register_mcp_prompts(server)`:**
1. Patches the low-level `server._mcp_server` handlers for `prompts/list` and `prompts/get`
2. On `prompts/list`: resolves connector → agent via `MCPConnectorService.resolve_connector_context()`, reads `agent.example_prompts`, parses each line, returns as `Prompt` objects (`name=slug`, `description=prompt_text`)
3. On `prompts/get`: looks up prompt by name (slug), returns a single user message `[{role: "user", content: {type: "text", text: prompt_text}}]`

**Helper functions:**
- `_get_agent_example_prompts()` — Resolves `mcp_connector_id_var` → connector → agent → `example_prompts` list. Returns `[]` if context is unavailable.
- `_parse_prompt_line(line)` — Splits on first `:`, returns `(slug, prompt_text)`. Lines without `:` use the full line as both name and text. Empty lines return `None`.

**Dynamic resolution:** Prompts are fetched from the DB on every call (same pattern as `resources.py`), so updates take effect immediately without server restart.

**Frontend editing:** "Example Prompts" button in the agent Config tab → `EditExamplePromptsModal` → textarea (one prompt per line) → saves as `example_prompts: [...]` via `AgentsService.updateAgent()`.

### Resource Change Notifications

**File:** `backend/app/mcp/notifications.py`

The MCP server sends `notifications/resources/list_changed` to connected clients after agent work or file uploads so they re-fetch workspace resources.

**Capability declaration:** `create_mcp_server_for_connector()` patches `_mcp_server.create_initialization_options` to include `NotificationOptions(resources_changed=True)`, causing the server to advertise `"resources": {"listChanged": true}` during the MCP initialize handshake.

**Session tracking:** `MCPServerRegistry` maintains `_active_sessions: dict[str, dict[str, ServerSession]]` mapping `connector_id -> {mcp_session_id -> ServerSession}`. Methods:
- `register_session(connector_id, mcp_session_id, session)` — called by the `send_message` tool wrapper after each call
- `get_sessions_for_connector(connector_id)` — used by `broadcast_resource_list_changed()`
- Cleaned up by `remove()` and `clear()`

**Notification helpers:**
- `notify_resource_list_changed(session)` — send via a specific session (used by tool handlers)
- `broadcast_resource_list_changed(connector_id)` — send to ALL sessions of a connector (used by upload route); catches exceptions per session

**Notification triggers:**
| Trigger | Method | Location |
|---------|--------|----------|
| After `send_message` completes | `ctx.session.send_resource_list_changed()` | `tools.py` (send_message wrapper) |
| After file upload succeeds | `broadcast_resource_list_changed(connector_id)` | `upload_routes.py` |

All notification failures are non-fatal — they are caught and logged at debug level to avoid affecting tool responses or upload results.

### Configuration

**File:** `backend/app/core/config.py`

| Setting | Default | Description |
|---------|---------|-------------|
| `MCP_SERVER_BASE_URL` | `""` | Base URL for MCP server endpoints (e.g., `https://tunnel.example.com/mcp`) |

## Frontend Implementation

### OAuth Consent Page

**File:** `frontend/src/routes/oauth/mcp-consent.tsx`

**Route:** `/oauth/mcp-consent` (public route, outside `_layout`)

**Flow:**
1. Reads `nonce` from query params
2. `beforeLoad`: Redirects to login if not authenticated (with return URL)
3. Fetches consent info via `GET /api/v1/mcp/consent/{nonce}`
4. Displays: agent name, connector name/mode, client name, scopes
5. "Authorize" button → `POST /api/v1/mcp/consent/{nonce}/approve` → redirects to client callback
6. "Deny" button → redirects to client callback with `error=access_denied`
7. Error states: expired nonce, already used, email not allowed

**UI:** Card layout with ShieldCheck icon, info grid, Authorize/Deny buttons.

### Connector Management Card

**File:** `frontend/src/components/Agents/McpConnectorsCard.tsx`

**Location:** Rendered in `AgentIntegrationsTab.tsx`

**Features:**
- List existing connectors with name, mode badge, active/inactive status
- Copyable MCP Server URL — constructed dynamically from `mcp_server_base_url` (returned by list endpoint) + connector ID, so URLs always reflect the current backend `MCP_SERVER_BASE_URL` env without stale cache
- Edit dialog with read-only MCP server URL, name/mode/email editing
- Create dialog: name, mode selector, allowed emails input
- Toggle active/inactive
- Delete with confirmation dialog

### State Management

**Query Keys:**
- `["mcp-connectors", agentId]` — List of connectors

**Mutations:**
- Create → invalidates connector list
- Delete → invalidates connector list
- Toggle active → invalidates connector list

### API Client

Uses direct `fetch()` calls with JWT auth headers (not auto-generated client).

**Interface:** `McpConnector` defined locally in `McpConnectorsCard.tsx`.

**Dynamic URL Construction:** The list endpoint returns `mcp_server_base_url` alongside connector data. The frontend constructs MCP server URLs as `{mcp_server_base_url}/{connector_id}/mcp` at render time via `getMcpServerUrl()`, rather than relying on the per-connector `mcp_server_url` field. This ensures URLs always reflect the current backend environment, even if React Query serves cached connector data.

## Security Features

**OAuth 2.1 Compliance:**
- PKCE S256 code challenge verification
- Client secret stored as SHA256 hash only
- Opaque bearer tokens (not JWT — no client-side decoding)
- Auth codes single-use with 5-minute expiry
- Token revocation support (RFC 7009)

**Access Control:**
- Connector CRUD: agent ownership validation
- Consent approval: email ACL check (owner or in `allowed_emails`)
- Token verification: per-connector isolation (token for connector A rejected on connector B)
- DCR: `max_clients` limit per connector (429 if exceeded)

**Session Isolation:**
- Session routing is driven by `context_id` (platform session UUID), not by `mcp_session_id`
- `context_id` is cross-connector verified: a session from connector A is rejected when used with connector B
- Invalid/garbage `context_id` values gracefully create a new session
- Each connector gets its own platform sessions
- Deactivated connector returns 404 on MCP requests
- Connector deletion cascades to OAuth clients, auth codes, tokens

## Implementation Scenarios

### Scenario 1: MCP Client Connects to Agent

```
1. Agent owner creates MCP connector in UI
2. Copies MCP Server URL: {MCP_SERVER_BASE_URL}/{connector_id}/mcp
3. Pastes URL into Claude Desktop / Cursor config
4. MCP client discovers AS via /.well-known/oauth-authorization-server
5. MCP client registers via DCR (POST /mcp/oauth/register)
6. MCP client initiates OAuth: GET /mcp/oauth/authorize
7. User redirected to consent page in browser
8. User logs in (if needed) and approves
9. Auth code exchanged for access + refresh tokens
10. MCP client calls send_message tool with bearer token
11. Agent processes message and streams response back
```

### Scenario 2: Multi-Turn Conversation via MCP

```
1. Chat 1 — first send_message(message, context_id="")
   → Creates new platform session
   → Returns {"response": "...", "context_id": "<session-uuid>"}
2. Chat 1 — LLM echoes back: send_message(message, context_id="<session-uuid>")
   → Looks up session by context_id → found → reuses it
   → Agent maintains conversation context across turns
3. Chat 2 — new chat: send_message(message, context_id="")
   → No context_id → creates a new session
   → Fully isolated from Chat 1, even though same MCP transport session
```

**Why `context_id` instead of `mcp_session_id`:** Claude Desktop reuses a single MCP transport session (`mcp-session-id` header) across all chats. The `context_id` parameter — echoed by the LLM from the tool response — is the only way to distinguish which chat a tool call originated from.

### Scenario 3: Connector Deactivation

```
1. Owner toggles connector to inactive
2. MCPConnectorService evicts MCP server from registry
3. Existing tokens remain in DB but:
   - MCPTokenVerifier rejects tokens (connector inactive check)
   - MCPServerRegistry returns 404 for connector path
4. MCP clients receive errors, must wait for reactivation
```

### Scenario 4: Token Refresh Flow

```
1. Access token expires after 1 hour
2. MCP client calls POST /mcp/oauth/token with grant_type=refresh_token
3. Server validates client credentials + refresh token
4. New access token issued (1 hour)
5. MCP client continues with new token
```

## File Locations Reference

### Backend — Models

- `backend/app/models/mcp_connector.py` — MCPConnector model and schemas
- `backend/app/models/mcp_oauth_client.py` — MCPOAuthClient model
- `backend/app/models/mcp_auth_code.py` — MCPAuthCode + MCPAuthRequest models
- `backend/app/models/mcp_token.py` — MCPToken model
- `backend/app/models/session.py:46-50` — MCP fields on session table
- `backend/app/models/__init__.py` — Exports added

### Backend — Routes

- `backend/app/api/routes/mcp_connectors.py` — Connector CRUD endpoints
- `backend/app/api/routes/mcp_consent.py` — OAuth consent endpoints
- `backend/app/mcp/oauth_routes.py` — OAuth AS endpoints (DCR, authorize, token, revoke)
- `backend/app/api/main.py` — Router registration (mcp_connectors, mcp_consent)
- `backend/app/main.py:208-213` — MCP OAuth router + registry mounting

### Backend — Services

- `backend/app/services/mcp_connector_service.py` — Connector CRUD logic

### Backend — MCP Infrastructure

- `backend/app/mcp/__init__.py` — Package init
- `backend/app/mcp/server.py` — FastMCP factory, MCPServerRegistry, mcp_connector_id_var
- `backend/app/mcp/token_verifier.py` — MCPTokenVerifier
- `backend/app/mcp/tools.py` — MCP-specific entry point, register_mcp_tools()
- `backend/app/mcp/resources.py` — Workspace resources, WorkspaceResourceManager, register_mcp_resources()
- `backend/app/mcp/prompts.py` — Agent example prompts, register_mcp_prompts()
- `backend/app/mcp/request_handler.py` — MCPRequestHandler (business logic, service layer delegation)
- `backend/app/mcp/notifications.py` — Resource change notification helpers (notify + broadcast)
- `backend/app/mcp/upload_routes.py` — File upload endpoint for MCP clients
- `backend/app/mcp/upload_token.py` — Temporary JWT creation/verification for file uploads
- `backend/app/mcp/oauth_routes.py` — Shared OAuth Authorization Server

### Backend — Configuration

- `backend/app/core/config.py` — `MCP_SERVER_BASE_URL` setting

### Backend — Migration

- `backend/app/alembic/versions/dc259404533e_add_mcp_integration_tables.py`

### Backend — Dependencies

- `backend/pyproject.toml` — `mcp[cli]` package added

### Frontend

- `frontend/src/routes/oauth/mcp-consent.tsx` — OAuth consent page
- `frontend/src/components/Agents/McpConnectorsCard.tsx` — Connector management card
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — Imports McpConnectorsCard

### Tests

- `backend/tests/api/mcp_integration/conftest.py` — Test fixtures
- `backend/tests/api/mcp_integration/test_mcp_connector_crud.py` — Connector CRUD tests
- `backend/tests/api/mcp_integration/test_mcp_oauth_flow.py` — OAuth flow tests
- `backend/tests/api/mcp_integration/test_mcp_send_message.py` — send_message tool handler tests
- `backend/tests/api/mcp_integration/test_mcp_file_upload.py` — File upload tool and endpoint tests
- `backend/tests/api/mcp_integration/test_mcp_resources.py` — Workspace resource tests (URI parsing, tree filtering, file reading, WorkspaceResourceManager, registration)
- `backend/tests/api/mcp_integration/test_mcp_prompts.py` — Agent example prompts tests (parsing, DB fetch, handler registration, list/get/not-found/empty)
- `backend/tests/api/mcp_integration/test_mcp_notifications.py` — Resource change notification tests (capability, send_message notification, broadcast, session tracking)
- `backend/tests/utils/mcp.py` — Test utilities

## Related Documentation

- `docs/agent_mcp_connector_concept.md` — Original concept document
- `docs/mcp-integration/implementation_plan.md` — Phased implementation plan

---

**Document Version:** 1.5
**Last Updated:** 2026-02-27
**Status:** Implemented (Phase 1-7 complete + workspace resources, per-chat session isolation via context_id, example prompts, resource change notifications)
