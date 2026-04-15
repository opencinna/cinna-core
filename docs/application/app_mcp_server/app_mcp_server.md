# App MCP Server

## Purpose

A universal, application-level MCP endpoint that any authenticated platform user can connect to. Unlike per-agent MCP connectors (which expose a single agent), the App MCP Server acts as a **router**: it receives a message, determines which agent should handle it, creates a session with that agent, and streams the response back -- all transparently to the MCP client.

Users connect once with a single URL and interact with multiple agents through natural language, without needing to configure separate MCP connectors for each agent.

## Core Concepts

| Term | Definition |
|------|-----------|
| **App Agent Route** | A binding between an agent and one or more users, with routing rules that determine when to use that agent |
| **Shared Route** | Route created by any user for their own agent (or by a superuser for any agent) and assigned to specific users; users can enable/disable their assignment |
| **Personal Route** | Legacy route type created via Settings (soft-deprecated; replaced by agent-scoped routes) |
| **Route Assignment** | Link between a route and a specific user, with a per-user enable/disable toggle |
| **Trigger Prompt** | Natural language description of when to route messages to this agent (fed to the AI router) |
| **Message Pattern** | Glob-style pattern (fnmatch) for auto-matching messages to agents (e.g., `sign this document *`) |
| **Effective Route** | Unified representation of a route (shared or personal) that is active for a given user and channel |
| **Routing Result** | Output of the routing engine: which agent was selected and how (pattern, AI, or single-route) |
| **Channel** | Delivery mechanism for the App MCP Server (currently `app_mcp`; extensible to `google-chat`, `slack`, etc.) |
| **auto_enable_for_users** | Flag on a route that, when set by a superuser, automatically enables assignments for newly assigned users |
| **activate_for_myself** | Flag on route creation that auto-adds the creator as an assigned user with `is_enabled=True`; defaults ON in the UI |

## User Stories / Flows

### Any Agent Owner: Add Agent to App MCP Server

1. User opens their agent's Integrations tab
2. Finds the "MCP Connectors" card and clicks "New"
3. Dialog shows two options: "Direct MCP Connector" and "App MCP Server Integration"
4. User selects "App MCP Server Integration"
5. Name defaults to the agent's name; fills in trigger prompt, session mode, and optionally message patterns
6. "Activate for Myself" switch is ON by default — the creator is automatically added as an assigned user with the route enabled
7. Optionally assigns other users via "Share with Users" (the creator is excluded from this list since they have the dedicated switch)
8. If superuser: can toggle "Make Active for Users" ON so assigned users get the route pre-enabled
9. If regular user: "Make Active for Users" toggle is disabled; assigned users must manually enable it
10. Clicks Save — route appears in the MCP Connectors card under "App MCP Server" section

### User: Connect via App MCP

1. User opens Settings > Channels tab > "MCP Server" card
2. Copies the MCP Server URL displayed in the card header (or clicks the `?` help button for step-by-step instructions via the Getting Started modal)
3. Pastes URL into MCP client (Claude Desktop, Cursor, etc.) as a new MCP connector
4. Clicks "Authorize" or "Connect" (label varies by client) to complete the OAuth flow
5. User types a message in MCP client chat
6. MCP client calls the `send_message` tool
7. App MCP Server routes message to the right agent > session created > response streamed
8. Subsequent messages in the same chat reuse the session (via `context_id`)

### User: Enable/Disable Shared Routes

1. User opens Settings > Channels tab > "MCP Server" card
2. Sees the "MCP Shared Agents" section listing routes shared with them
3. Each row shows: agent name, owner name, "shared by" info (if different from owner), and an enable/disable switch
4. Toggles the switch to enable or disable their assignment
5. If the route creator has globally disabled the route, the switch is greyed out with a "Disabled by admin" label

### Superuser: Manage Routes Across All Agents

Superusers retain full management capabilities via the preserved admin API endpoints at `/api/v1/admin/app-agent-routes/`. They can list all routes across all agents, create routes for any agent, and set `auto_enable_for_users=True` so that assigned users get the route immediately active.

## Business Rules

### Route Ownership and Creation

- **Any user** can create App MCP routes for agents they own
- **Superusers** can create routes for any agent regardless of ownership
- Non-superusers cannot set `auto_enable_for_users=True` — this is superuser-only
- Non-superusers can assign other users to their routes, but assignments are created with `is_enabled=False` (users must manually enable)
- Superusers assigning users to routes with `auto_enable_for_users=True` create assignments with `is_enabled=True`
- When `activate_for_myself=True` (default in UI), the route creator is auto-added as an assigned user with `is_enabled=True`
- The creator is excluded from the "Share with Users" picker in the UI to avoid confusion (they use the dedicated "Activate for Myself" switch instead)

### Route Resolution

- **Shared routes** (AppAgentRoute via assignment) are active for a user only when: route `is_active=true` AND assignment `is_enabled=true`
- **Personal routes** (legacy UserAppAgentRoute) are active when: route `is_active=true`
- Multiple routes per agent are supported — no unique constraint by owner

### Message Routing Priority

1. **Single route shortcut** -- if user has only one effective route, use it directly (no classification needed)
2. **Pattern matching** -- try each route's `message_patterns` against the message using fnmatch (case-insensitive); first match wins
3. **AI classification** -- call LLM with the message and all available routes' trigger prompts; LLM picks the best agent or returns "NONE"
4. **No match** -- return error asking user to be more specific

### Message Transformation

When the AI router classifies a message, it also **strips routing prefixes** and extracts the core task. This ensures agents receive clean, actionable messages instead of delegation phrasing.

**How it works:**
- The AI router returns both a selected agent ID and a transformed message
- Routing prefixes like "ask cinna to...", "tell john to...", "forward to X..." are stripped automatically
- If the message has no routing prefix (it's already a direct task), no transformation occurs
- Pattern-match and single-route-shortcut paths deliver the original message unchanged (no AI involved)

**Examples:**
- "ask cinna to generate employee report" → agent receives "generate employee report"
- "tell john to fix the bug" → agent receives "fix the bug"
- "generate report" → agent receives "generate report" (no prefix, no change)

**Two-level transformation (with Identity MCP):**
- "ask cinna to ask john to generate report" → Stage 1 strips one layer → "ask john to generate report" → Stage 2 strips another → agent receives "generate report"
- Each routing stage transforms the output of the previous stage

**Cascade logic:**
- If both stages transform, the final (Stage 2) transformation is used
- If only Stage 1 transforms, its output is used
- If neither transforms, the original message is used

**Safety guards:**
- Empty or whitespace-only transformations are discarded (original used)
- Transformations identical to the original are discarded
- Transformations exceeding 2x the original message length are discarded (prevents hallucinated expansions)

**Auditability:** When a transformation occurs, the original message is stored in `session_metadata["app_mcp_original_message"]` for traceability

### Session Management

- First message creates a new session with `integration_type = "app_mcp"`
- Response includes `context_id` (the session UUID)
- Subsequent messages with the same `context_id` reuse the existing session (no re-routing)
- `context_id` is validated: must belong to the authenticated user and have `integration_type = "app_mcp"`
- Invalid or missing `context_id` triggers a new routing + session creation
- Sessions are independent of route configuration once created -- deleting a route does not affect active sessions

### OAuth / Authentication

- Reuses the existing shared OAuth AS at `/mcp/oauth/...`
- MCP clients register via the same DCR endpoint with `resource` pointing to `/app/mcp`
- Consent page shows "Application MCP Server" instead of a specific agent name
- Any authenticated platform user can approve (no email ACL needed -- the user IS the ACL)
- Tokens are app-scoped (stored in `app_mcp_token`), not connector-scoped

### Access Control

- **Agent-scoped endpoints** (`/agents/{agent_id}/app-mcp-routes`): accessible to agent owner and superusers
- **Admin endpoints** (`/admin/app-agent-routes`): superuser-only, provides cross-agent visibility
- **User endpoints** (`/users/me/app-agent-routes`): regular auth, returns user's shared and personal routes
- Session ownership enforced: session must belong to the authenticated user
- Cross-user session hijacking prevented: `context_id` is verified against `user_id`

## Architecture Overview

```
MCP Client (Claude Desktop, Cursor, etc.)
    |
    |  OAuth 2.1 (shared AS at /mcp/oauth/...)
    v
App MCP Server (/mcp/app/mcp)
    |
    +-- Authenticate user (AppMCPTokenVerifier)
    +-- Receive send_message(message, context_id)
    |
    +-- 1. If context_id exists -> reuse existing session
    |
    +-- 2. Pattern matching: try message_patterns on user's active routes
    |      -> If match found -> select that agent
    |
    +-- 3. AI router: call LLM with message + user's available agents
    |      -> LLM picks the best agent + extracts core task (strips routing prefixes)
    |
    +-- 4. Create session with selected agent, send transformed message, stream response
    |
    +-- Return { response, context_id, agent_name }

Agent Owner UI:  Agent > Integrations tab > MCP Connectors card > New > App MCP Server Integration
User Settings:   Settings > Channels tab > "MCP Server" card (read view + toggle shared routes)
```

### Integration with Per-Agent MCP

| Component | Per-Agent MCP | App MCP Server |
|-----------|--------------|----------------|
| OAuth AS | Shared `/mcp/oauth/...` | Same shared AS |
| Resource Server URL | `/mcp/{connector_id}/mcp` | `/mcp/app/mcp` |
| Token Verifier | `MCPTokenVerifier(connector_id)` | `AppMCPTokenVerifier()` |
| MCPServer instance | One per connector (lazy) | Single instance |
| Session routing | `context_id` > fixed agent | `context_id` > routed agent |
| Registry | `MCPServerRegistry` dispatches by connector UUID | Special `"app"` path handled before UUID validation |

## Integration Points

- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** -- reuses the shared OAuth AS, MCPServerRegistry, and session infrastructure
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** -- App MCP sessions use the same Session model with `integration_type = "app_mcp"` and `agent_id` for direct agent resolution
- **[AI Functions](../../development/backend/ai_functions_development.md)** -- the AI router uses `AIFunctionsService.route_to_agent()` with `gemini-2.5-flash-lite` for message classification
- **[Agent Management](../agent_management/agent_management.md)** -- routes reference agents by ID; any user can create routes for their own agents, superusers for any agent

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No routes configured for user | Error: "No agents are configured for your account. Contact your admin." |
| AI router can't determine agent | Error: "Could not determine which agent to use. Please be more specific." |
| Agent environment not active | Environment auto-activates; pending until ready |
| Agent deleted after session created | Error: "The agent for this conversation is no longer available" |
| Cross-user `context_id` | Session not found (falls through to new routing) |
| Concurrent messages on same session | Error: "Another message is being processed. Please wait." |
| Non-superuser sets `auto_enable_for_users=True` | 400: "Only administrators can auto-enable routes for users" |
| Non-superuser creates route for agent they don't own | 403: "You can only create routes for your own agents" |
| Non-superuser edits or deletes another user's route | 403: "Access denied" |

## MCP Prompts

The App MCP Server exposes the user's active route trigger prompts as MCP prompts via `prompts/list`. This allows external AI clients (like Claude Desktop) to discover available agents without guessing. Each prompt includes the route name and trigger description.

## Deprecation Notes

### Personal Routes (UserAppAgentRoute)

The `user_app_agent_route` table and personal route creation in Settings are **soft-deprecated**. Existing personal routes continue to work and appear in the Settings card under a "Personal Routes" section with a deprecation hint. New routes should be created via the agent's Integrations tab (agent-scoped endpoints). The personal route creation UI has been removed from `AppAgentRoutesCard`.

### Admin Application Agents Page

The dedicated admin "Application Agents" page (`/admin/application-agents`) and its sidebar menu item have been removed. Superusers manage routes from each agent's Integrations tab, with superuser-only admin endpoints still available via the API for administrative scripting.
