# Identity MCP Server

## Purpose

The Identity MCP Server introduces a **person-level abstraction** on top of the App MCP Server routing system. Instead of exposing individual agents directly, users expose themselves as a routable "identity" — a virtual contact point that other users can address by name.

When a message is routed to an identity, a **two-stage routing** process runs: Stage 1 resolves the message to a person (identity owner), and Stage 2 selects the appropriate agent from that person's portfolio, filtered to only those accessible to the specific caller.

This lets callers address colleagues naturally ("ask User B to prepare the annual report") without knowing which agents exist behind the identity.

## Glossary

| Term | Definition |
|------|-----------|
| **Identity** | A user's personal routing endpoint — the set of agents they expose to other users, controlled per-caller |
| **Identity Owner** | The user who configures and exposes their identity (e.g., User B) |
| **Caller** | The user who sends messages addressed to an identity owner (e.g., User A) |
| **Identity Agent Binding** | Configuration linking one of the owner's agents to their identity, with a trigger prompt and optional caller access list |
| **Binding Assignment** | Per-caller access record linking a specific binding to a target user; carries per-caller and per-owner toggles |
| **Stage 1 Routing** | The App MCP Server's existing routing engine — resolves a message to a person or direct agent |
| **Stage 2 Routing** | Identity-specific routing — runs after Stage 1 selects a person, picks the right agent from their accessible bindings |
| **Identity Session** | A session created in the identity owner's space, with `integration_type = "identity_mcp"` and `identity_caller_id` tracking the caller |

## Two-Stage Routing Flow

```
Caller (User A): "Ask User B to prepare the annual report"
       |
       v
Stage 1: App MCP Server Router
  - Effective routes include agent routes AND identity contacts
  - AI classifies message -> selects "User B" (identity route)
  - Transforms message: "prepare the annual report" (strips "Ask User B to")
       |
       v
Stage 2: Identity Router (user B's identity, filtered to User A's access)
  - Receives Stage 1's transformed message: "prepare the annual report"
  - AI classifies -> selects "Annual Report Agent"
  - No further prefix to strip, so transformation is null
       |
       v
Session created in User B's space (user_id = User B)
identity_caller_id = User A
Agent receives: "prepare the annual report"
Response streamed back to User A's MCP client
```

Stage 2 only considers agents where the caller has an active, enabled binding assignment. If User B has three agents in their identity but only two are shared with User A, Stage 2 sees only those two.

**Message transformation across stages:** Each routing stage can strip one layer of routing prefixes. Stage 1's transformed output becomes Stage 2's input. For example, "ask cinna to ask john to generate report" → Stage 1 produces "ask john to generate report" → Stage 2 produces "generate report". See [App MCP Server — Message Transformation](../app_mcp_server/app_mcp_server.md#message-transformation) for full details.

## User Stories / Flows

### Identity Owner: Expose an Agent via Identity (from Integrations Tab)

1. Agent owner opens their agent's Integrations tab
2. Clicks "New" in the MCP Connectors card
3. Selects "Identity MCP Server Integration" (the third option)
4. Writes a trigger prompt describing when to route to this agent (e.g., "Handle annual report requests and financial analysis tasks")
5. Selects session mode (conversation or building)
6. Searches and selects users to share this agent with via the "Share with Users" picker
7. Clicks "Add to Identity" — creates the binding and assignments
8. Selected users now see the identity owner in their "Identity Contacts" section

### Identity Owner: Manage Identity from Settings

1. Opens Settings > Channels tab
2. Sees the "Identity Server" card showing all agents in their identity
3. Each agent row shows: agent name, trigger prompt, session mode icon, active/inactive badge, and a chevron to expand user assignments
4. Expanding a row shows which users have access to that agent (as clickable pill badges with a remove button)
5. Can edit trigger prompt, message patterns, and session mode via the edit dialog
6. Can toggle individual agents active/inactive with a switch
7. Can remove agents from identity (cascades all assignments)
8. Can add new agents via the "Add Agent" button, which opens an inline form

### Caller: Enable and Use an Identity Contact

1. Opens Settings > Channels tab > "MCP Server" card
2. Sees the "Identity Contacts" section (after the "MCP Shared Agents" section)
3. Each row shows an identity owner's name, email, and an enable/disable toggle
4. Enables the desired contact
5. In their MCP client, types a message addressing that person: "Ask User B to prepare the annual report"
6. Stage 1 routes to User B's identity; Stage 2 selects the best matching agent
7. Response streams back with `agent_name` set to User B's name (not the internal agent name)
8. Subsequent messages with the same `context_id` continue the conversation with the same agent

### Identity Owner: View Identity Sessions

1. Sees new sessions appearing in their agent's session list as normal
2. Session header shows: "Via Identity — initiated by {caller_name}"
3. Session behaves like any other agent session — no special owner controls

## Business Rules

### Identity Configuration

- Users have one identity; it is workspace-independent (agents from any workspace can be added)
- Each agent binding has its own trigger prompt and optional glob message patterns for Stage 2 routing
- The same agent cannot be bound twice to the same identity (unique constraint: `owner_id, agent_id`)
- Agents must be owned by the identity owner — you can only expose your own agents
- Identity does not generate routes for the owner themselves (self-exclusion at the assignment level)

### Per-Caller Access

- Each binding assignment links a specific binding to a specific target user
- Different callers see different subsets of the owner's agents behind the same identity
- A caller only sees an identity owner as addressable if they have at least one active and enabled binding assignment
- If no accessible bindings exist for a caller, Stage 2 returns an error rather than routing to a random agent

### Toggles and Visibility

- **Owner-level toggle** (`is_active` on binding): disables an agent for all callers at once
- **Owner-level toggle** (`is_active` on assignment): disables a specific agent for a specific caller
- **Caller-level toggle** (`is_enabled` on assignment): caller can opt out of a specific identity owner entirely (per-person toggle affects all bindings from that owner)
- A caller-level toggle is per-person: enabling/disabling affects all binding assignments from that owner to that caller simultaneously
- `auto_enable`: if set by a superuser when creating assignments, `is_enabled` starts as `True` (bypasses the caller's opt-in requirement)

### Stage 2 Routing Priority

1. **Single binding shortcut** — if the caller has access to only one binding, use it directly (no classification needed)
2. **Pattern matching** — try each binding's `message_patterns` against the message using fnmatch (case-insensitive, newline-separated patterns); first match wins
3. **AI classification** — call LLM with the message and each binding's trigger prompt; LLM picks the best agent or returns "NONE"
4. **No match** — error returned to caller

### Session Ownership and Access

- Identity sessions are owned by the identity owner (`user_id = owner_id`), not the caller
- The caller is tracked via `identity_caller_id` (indexed column)
- The caller communicates exclusively through their MCP client — they have no platform UI access to the session
- The owner sees identity sessions in their session list like normal sessions
- Response payload returns `agent_name` set to the identity owner's name, not the internal agent's name

### Session Continuity and Binding Validity

- The caller receives a `context_id` (session UUID in the owner's space) for session continuity
- On each subsequent message, the system validates `session.identity_caller_id` (not `session.user_id`) for resumption
- Before processing each message on an identity session, the system verifies:
  - The identity binding (`identity_binding_id`) still exists and `is_active = True`
  - The binding assignment (`identity_binding_assignment_id`) still exists with `is_active = True` and `is_enabled = True`
  - If either check fails: "This identity connection is no longer active."
- This is stricter than regular App MCP sessions, which survive route deletion

## Integration Points

- **[App MCP Server](../app_mcp_server/app_mcp_server.md)** — identity contacts appear as entries in `get_effective_routes_for_user()` alongside direct agent routes; `AppMCPRoutingService.route_message()` invokes Stage 2 routing when a selected route has `source = "identity"`
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — identity sessions use the same `Session` model with `integration_type = "identity_mcp"` and three additional columns: `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — uses the same shared OAuth AS and App MCP token infrastructure; no separate OAuth flow
- **[Agent Management](../agent_management/agent_management.md)** — MCP Connectors card is extended with a third integration type option for identity registration

## Access Control

| Action | Who |
|--------|-----|
| Create/edit/delete identity agent bindings | Agent owner only (binding.owner_id == current_user.id) |
| Assign users to a binding | Binding owner only |
| Set `auto_enable = True` on assignment | Superuser only |
| Enable/disable received identity contact | Target user (per-person toggle on their own assignments) |
| View own identity bindings and assignments | Identity owner only |
| View received identity contacts | Target user only |
| Route messages to an identity | Any user with at least one active, enabled binding assignment |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No accessible bindings for caller | Stage 2 returns error: routing fails gracefully |
| Stage 2 AI cannot determine agent | Error: "Could not determine which of {owner_name}'s agents to use. Please be more specific." |
| Binding disabled mid-conversation | Next message returns: "This identity connection is no longer active." |
| Assignment disabled or removed mid-conversation | Same: "This identity connection is no longer active." |
| Agent deleted (binding cascade) | Binding and all assignments are deleted; caller's effective routes no longer include this person if no other bindings remain |
| Self-assignment attempt | Silently skipped (self-exclusion) |
| Non-superuser sets `auto_enable = True` | 403: "Only administrators can auto-enable identities for users" |
| Duplicate binding (`owner_id, agent_id`) | 409: "Agent already added to identity" |
| Agent not owned by user | 403: "You can only add your own agents to your identity" |
| Cross-caller context_id use | Falls through to new routing (security; `identity_caller_id` must match) |
