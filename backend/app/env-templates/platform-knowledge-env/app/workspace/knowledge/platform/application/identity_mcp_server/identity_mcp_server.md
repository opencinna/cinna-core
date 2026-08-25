# Identity MCP Server

> **Naming note — the directory name is behind the concept, deliberately.** This doc still lives under `identity_mcp_server/`, but identity is no longer an App-MCP-only mechanism: since Phase 3 of the channels & identity unification refactor it is a **routing-layer** concept, and Server Channels is its second consumer. Renaming the directory to `identity_routing/` was considered for that phase and **deferred to Phase 7**, which owns the consistency sweep. The reasoning is worth recording rather than leaving as an omission: a rename means updating every inbound link, including the env-template knowledge mirror an agent reads at runtime, and Phase 5 deletes the `AppAgentRoute` family — which forces another pass over these same files anyway. Doing that pass twice is worse than doing it once, at the end.

## Purpose

An **identity** is a person-shaped routing candidate: instead of exposing individual agents directly, a user exposes *themselves* as a routable contact point that other users can address by name. Picking that person is one routing decision; picking which of their agents answers is a second one.

Identity is a **routing-layer** concept, not a property of any one surface. `IdentityCandidateProvider` builds "the people this caller may address" as a candidate list, and each surface composes it into its own ballot — providers compose, and no surface borrows another's candidate set or enablement toggles. Two surfaces consume it today:

| Surface | Ballot composition | Gate | Resulting session |
|---------|-------------------|------|-------------------|
| [App MCP Server](../app_mcp_server/app_mcp_server.md) | the caller's own eligible agents (`ChannelCandidateProvider`) **+** identity candidates | the per-person contact toggle only | `integration_type = "identity_mcp"` |
| [Server Channels](../server_channels/server_channels.md) | the sender's own agents **+** identity candidates | the sender's per-channel `allow_identity_routing` opt-in (default off, never inherited) **and** the per-person contact toggle | `integration_type = "channel_<type>"` — an identity-routed channel session is still a channel session |

Whichever surface asked, a message that resolves to a person then runs **two-stage routing**: Stage 1 resolves the message to a person (identity owner), and Stage 2 selects the appropriate agent from that person's portfolio, filtered to only those accessible to the specific caller.

This lets callers address colleagues naturally — "ask User B to prepare the annual report" from an MCP client, or "hey, ask HR what my time-off status is" from Google Chat — without knowing which agents exist behind the identity.

**Two stages is also the recursion cap.** An identity's bindings name agents, never further identities, so there is no depth to count and no cycle to guard. That is the intended structure, not an omission — a depth counter here would imply identity chains are supported.

## Glossary

| Term | Definition |
|------|-----------|
| **Identity** | A user's personal routing endpoint — the set of agents they expose to other users, controlled per-caller |
| **Identity Owner** | The user who configures and exposes their identity (e.g., User B) |
| **Caller** | The user who sends messages addressed to an identity owner (e.g., User A) |
| **Identity Agent Binding** | Configuration linking one of the owner's agents to their identity, with a trigger prompt and optional caller access list |
| **Binding Assignment** | Per-caller access record linking a specific binding to a target user; carries per-caller and per-owner toggles |
| **Stage 1 Routing** | The consuming surface's own routing pass, whose ballot includes identity candidates — resolves a message to a person or to a directly addressable agent. App MCP Stage 1 for MCP clients; channel Pass 1 for Server Channels |
| **Stage 2 Routing** | Identity-specific routing (`IdentityRoutingService.route_within_identity`) — runs after Stage 1 selects a person, picks the right agent from that caller's accessible bindings. `match_method` is `"only_one"` or `"ai"` |
| **Identity Session** | A session created in the identity owner's space, with `identity_caller_id` tracking the caller. `integration_type` is `"identity_mcp"` on the App MCP path and `"channel_<type>"` on the channel path — the transport, not the routing story, names it |
| **Identity Candidate** | One ballot entry per identity *owner*, not per binding, carrying the namespaced `ref_id` `identity:{owner_id}` so a person can never be looked up as an agent |
| **Identity Grant** | The three ids (`owner_id`, `binding_id`, `assignment_id`) Stage 2 hands back on the channel path. A *claim* the ingestion layer re-verifies against the database, never a conclusion |
| **Prompt Examples** | Optional newline-separated short prompts on an `IdentityAgentBinding`, aggregated and prefixed with the owner's name for MCP client discovery |

## Two-Stage Routing Flow

### From an MCP client (App MCP Server)

```
Caller (User A): "Ask User B to prepare the annual report"
       |
       v
Stage 1: App MCP Server Router
  - The ballot is composed: the agents the caller owns (ChannelCandidateProvider)
    PLUS one candidate per identity owner they can address (IdentityCandidateProvider)
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

### From a chat app (Server Channels)

```
Sender (whitelisted Google Chat user): "hey, ask HR what my time-off status is?"
       |
       v
Channel policy resolved once for this message (ChannelPolicyService)
  - is the channel available to this sender?
  - agent scope, pinned agent, allow_auto_install
  - allow_identity_routing  <-- the sender's own opt-in, default off, never inherited
       |
       v
Stage 1: Channel routing Pass 1
  - ChannelCandidateProvider.build  -> the sender's own in-scope agents
  - IdentityCandidateProvider.build -> one candidate per person they may address
    (ONLY when allow_identity_routing is on; when it is off the provider is
     not called at all, so no identity even appears in the trace)
  - AI classifies -> selects "HR" (an identity:{owner_id} candidate)
       |
       v
Stage 2: Identity Router (HR's identity, filtered to the sender's access)
  - picks HR's time-off agent, returns an IdentityGrant (owner, binding, assignment)
  - Pass 2 (catalog auto-install) does NOT run: the sender addressed a person
       |
       v
ChannelInboundService binds the thread and ingests
  - ChannelThreadBinding.user_id  = the SENDER   (whose thread is this)
  - session.user_id               = HR           (whose workspace is answering)
  - identity_caller_id            = the sender
  - integration_type              = channel_google_chat  (NOT identity_mcp)
       |
       v
HR's agent answers -> ChannelOutboundService resolves the binding by session id
                   -> reply lands back in the sender's Google Chat thread
```

Stage 2 only considers agents where the caller has an active, enabled binding assignment. If User B has three agents in their identity but only two are shared with User A, Stage 2 sees only those two.

**Message transformation across stages:** Each routing stage can strip one layer of routing prefixes. Stage 1's transformed output becomes Stage 2's input. For example, "ask cinna to ask john to generate report" → Stage 1 produces "ask john to generate report" → Stage 2 produces "generate report". See [App MCP Server — Message Transformation](../app_mcp_server/app_mcp_server.md#message-transformation) for full details.

## User Stories / Flows

### Identity Owner: Expose an Agent via Identity (from Settings)

**As of Phase 5 of the channels & identity unification, this is the only creation path.** The agent's Integrations tab MCP Connectors "New" dialog no longer offers an "Identity MCP Server Integration" option — it now offers exactly two choices, Direct MCP Connector and (developer-only) Agent to Agent MCP Connector. See [App MCP Server](../app_mcp_server/app_mcp_server.md) and [Agent Management](../agent_management/agent_management.md#mcp-connectors).

1. Agent owner opens **Settings → Channels → Identity Server** card
2. Clicks **"Add Agent"** in the card header
3. Picks one of their own agents via the agent selector dialog
4. Writes a trigger prompt describing when to route to this agent (e.g., "Handle annual report requests and financial analysis tasks")
5. Selects session mode (conversation or building)
6. Searches and selects users to share this agent with via the "Share with Users" picker
7. Saves — creates the binding and assignments
8. Selected users now see the identity owner in their "Identity Contacts" section

### Identity Owner: Manage Identity from Settings

1. Opens Settings > Channels tab
2. Sees the "Identity Server" card showing all agents in their identity
3. Each agent row shows: agent name, trigger prompt, session mode icon, active/inactive badge, and a chevron to expand user assignments
4. Expanding a row shows which users have access to that agent (as clickable pill badges with a remove button)
5. Can edit trigger prompt, message patterns, and session mode via the edit dialog
6. Can toggle individual agents active/inactive with a switch
7. Can remove agents from identity (cascades all assignments)
8. Can add new agents via the "Add Agent" button, which opens the "Add Agent to Identity" dialog (agent picker, trigger prompt, session mode, user share picker)

### Caller: Enable and Use an Identity Contact

1. Opens Settings > Channels tab and finds the identity contacts list on `UserChannelsCard` (as of Phase 5 the sole surface for this list — it used to be duplicated, without the consent copy, on the now-stripped `AppAgentRoutesCard` / "MCP Server" card)
2. Each row shows an identity owner's name, email, and an enable/disable toggle
4. Enables the desired contact
5. In their MCP client, types a message addressing that person: "Ask User B to prepare the annual report"
6. Stage 1 routes to User B's identity; Stage 2 selects the best matching agent
7. Response streams back with `agent_name` set to User B's name (not the internal agent name)
8. Subsequent messages with the same `context_id` continue the conversation with the same agent

### Caller: Address an Identity from a Server Channel

1. Opens Settings → Channels and expands the channel row (e.g. Google Chat)
2. Finds the **Identity routing** section and turns the master switch on. The copy states the consequence plainly, because it is neither obvious nor reversible after the fact: *your message and the whole conversation then live in that person's workspace, and they can read it*, and switching it back off stops future messages but does not take back one already sent
3. Below the switch is the list of people who have shared their identity with them, each with the **same person-level toggle** the App MCP surface uses (`IdentityBindingAssignment.is_enabled`, via `GET/PATCH /users/me/identity-contacts/`) — one toggle governing every surface, not a second per-channel allowlist. The section says so explicitly, because turning someone off here also stops the caller addressing them from an MCP client
4. From the chat app, writes "hey, ask HR what my time-off status is?"
5. Pass 1 classifies HR onto the ballot and picks them; Stage 2 selects HR's time-off agent
6. The reply comes back in the sender's own chat thread, from a session that lives in HR's workspace
7. A UI affordance worth knowing: with identity routing on but every person switched off, the card warns that nothing will route to another person's agent — an "on but inert" state is otherwise silent

### Identity Owner: View Identity Sessions

1. Sees new sessions appearing in their agent's session list as normal
2. Session header shows a **"Via Identity — {caller_name}"** badge. On the App MCP path it hangs off `integration_type == "identity_mcp"`; on the channel path off `integration_type.startsWith("channel_")` plus `session_metadata.identity_caller_name`, because a channel session must keep its `channel_` prefix for the reply to be deliverable
3. Session behaves like any other agent session — no special owner controls

## Business Rules

### Identity Configuration

- Users have one identity; it is workspace-independent (agents from any workspace can be added)
- Each agent binding has its own trigger prompt for Stage 2 routing. `message_patterns` no longer exists on the binding at all — dropped in Phase 5's migration, alongside the `AppAgentRoute` deletion — see *Stage 2 Routing Priority* below
- The same agent cannot be bound twice to the same identity (unique constraint: `owner_id, agent_id`)
- Agents must be owned by the identity owner — you can only expose your own agents
- Identity does not generate routes for the owner themselves (self-exclusion at the assignment level)

### Trace Visibility of Identities

Every candidate a routing pass rejects is normally recorded with a `skip_reason`, because a candidate list showing only the finalists cannot diagnose the failure that actually bites. Identity has **one deliberate inversion** of that rule, and it exists in exactly one place:

- An identity owner the caller *can* address but who currently has nothing reachable (binding inactive, assignment inactive, or the caller's own contact toggle off) **is** recorded, as `SKIP_IDENTITY_UNAVAILABLE`.
- An identity owner the sender could have reached with `allow_identity_routing` **off** is **not** recorded at all — not even as a skip. With the switch off `ChannelRoutingService` never calls the provider, so no rows exist. Recording them would publish the existence of other people's identities into a trace an external sender can trigger at will, one row per person who has ever named them on a binding. The diagnosis is not lost, only moved: the sender's own Settings → Channels page says whether the switch is on, and that is the one control that changes the outcome.

A `SKIP_IDENTITY_UNAVAILABLE` row on a channel trace is therefore itself evidence that the sender's channel-level switch was already on.

### Per-Caller Access

- Each binding assignment links a specific binding to a specific target user
- Different callers see different subsets of the owner's agents behind the same identity
- A caller only sees an identity owner as addressable if they have at least one active and enabled binding assignment
- If no accessible bindings exist for a caller, Stage 2 returns an error rather than routing to a random agent

### Which Toggle Is Whose

This is the single easiest thing to get backwards, and the direction matters because one of these is a consent and the others are access control:

| Switch | Owned by | Means |
|--------|----------|-------|
| `IdentityAgentBinding.is_active` | the **identity owner** (receiver) | "this agent of mine is exposed at all" — the receiver's control over who may reach them |
| `IdentityBindingAssignment.is_active` | the **identity owner** (receiver) | "this specific person may reach this specific agent of mine" — also the receiver's control |
| `IdentityBindingAssignment.is_enabled` | the **caller** (sender) | the caller's own per-person opt-out of *addressing* that owner. The row is keyed by `target_user_id`, and `IdentityService.toggle_identity_contact` filters on `target_user_id == current_user.id`. It is **not** the receiver's gate |
| `channel_user_setting.allow_identity_routing` | the **sender** | the sender's per-channel consent that a message of theirs may be routed into somebody else's workspace, where they can read it. Never inherits from a channel default |

### Toggles and Visibility

- **Owner-level toggle** (`is_active` on binding): disables an agent for all callers at once
- **Owner-level toggle** (`is_active` on assignment): disables a specific agent for a specific caller
- **Caller-level toggle** (`is_enabled` on assignment): caller can opt out of a specific identity owner entirely (per-person toggle affects all bindings from that owner)
- A caller-level toggle is per-person: enabling/disabling affects all binding assignments from that owner to that caller simultaneously
- `auto_enable`: if set by a superuser when creating assignments, `is_enabled` starts as `True` (bypasses the caller's opt-in requirement)

### Stage 2 Routing Priority

1. **Single binding shortcut** (`match_method="only_one"`) — if the caller has access to only one binding, use it directly (no classification needed). This stays a Stage-2 property rather than being flattened into Stage 1: Stage 1 chose a *person*, and whether that person happens to have exactly one reachable agent is not a fact Stage 1's ballot can hold without re-deriving the caller's access for every candidate on it
2. **AI classification** (`match_method="ai"`) — call the shared `AgentClassifier` with the message and each binding's trigger prompt and prompt examples; it picks the best agent or returns "NONE"
3. **No match** — on the App MCP path an error is returned to the caller. On the channel path it is an ordinary `no_match` for the whole decision, not an error: the sender gets the existing "couldn't find an agent" reply, and Pass 2 (catalog auto-install) deliberately does **not** run — auto-installing a bundle for somebody who asked to speak to a colleague is not a better answer than saying nothing matched

`match_method` on a Stage-2 decision is therefore one of exactly two values, `"only_one"` or `"ai"`.

**Glob pattern matching was removed from Stage 2 in Phase 1, and the column itself is gone as of Phase 5.** It was a second routing mechanism with silently higher priority than the classifier, and no trace explained it well: a decision that a pattern took would look, in the trace, much like one the classifier took. The classifier is cheap and its reasoning is recorded. `IdentityAgentBinding.message_patterns` was dropped outright in Phase 5's migration, alongside the `AppAgentRoute` deletion.

### Session Ownership and Access

- Identity sessions are owned by the identity owner (`user_id = owner_id`), not the caller
- The caller is tracked via `identity_caller_id` (indexed column)
- The caller communicates exclusively through the surface they came in on — their MCP client, or their chat-app thread. They have no platform UI access to the session
- The owner sees identity sessions in their session list like normal sessions. On the channel path the session view renders a **"Via Identity — {caller}"** badge, sourced from `identity_caller_name` in `session_metadata`, because the owner would otherwise find a conversation they never started containing a stranger's message, identified only by a UUID in a column nothing renders
- On the App MCP path the response payload returns `agent_name` set to the identity owner's name, not the internal agent's name

### Session Ownership vs. Thread Ownership (channel path)

On Server Channels the two ownerships **diverge, deliberately**, and each answers a different question:

- **`ChannelThreadBinding.user_id` is the sender** — *whose thread is this?* Thread ownership is what stops one member of a group chat space from posting into another person's conversation; a different sender posting into a bound thread is still declined as "belongs to someone else", unchanged by identity.
- **`session.user_id` is the identity owner** — *whose workspace is answering?* The agent is theirs, runs on their credentials, in their space, and the session appears in **their** session list, not the sender's.

`ChannelThreadBinding.agent_id` therefore names an agent the binding's own user does not own. That is legitimate only because of the grant, and only for as long as the grant keeps verifying.

### Per-Message Re-Verification (channel path)

Nothing about an identity-routed channel thread is decided once:

- **The grant is re-read on every message.** On the first message the claim comes from Stage 2; on every later one it is rebuilt from the session row's three identity columns. Either way `ChannelIngestionService.assert_access` re-verifies all six conditions (`IdentityService.verify_identity_access`) against the database before anything is created, so an owner who deactivates a binding or an assignment mid-thread is honoured on the very next message rather than at the next new thread.
- **The sender's own consent is re-read on every message too**, from that message's single policy resolution. Turning `allow_identity_routing` off — or using "reset to defaults", which drops the settings row and returns the column to its `false` default — stops the *existing* identity thread on its next message. A consent switch that could not be withdrawn on the conversation it authorised would be no consent at all.
- **The decline is generic.** Both refusals raise a bare `PermissionError` that the inbound pipeline turns into the same detail-free reply every other failure gets — a reply that named the gate would be an oracle for an external sender. The binding then fails, and a failed binding self-heals: the next message deletes it and re-routes over the sender's **own** agents, so the thread is never bricked.
- **Recovery never invents a grant.** If the bound session was deleted, a continuing message arrives with no grant; on a foreign agent `assert_access` refuses rather than re-deriving an authorization the routing layer never issued.

### Session Continuity and Binding Validity

- The caller receives a `context_id` (session UUID in the owner's space) for session continuity
- On each subsequent message, the system validates `session.identity_caller_id` (not `session.user_id`) for resumption
- Before processing each message on an identity session, the system verifies:
  - The identity binding (`identity_binding_id`) still exists and `is_active = True`
  - The binding assignment (`identity_binding_assignment_id`) still exists with `is_active = True` and `is_enabled = True`
  - If either check fails: "This identity connection is no longer active."
- This is stricter than regular App MCP sessions, which survive route deletion

## Prompt Examples

Identity Agent Bindings support prompt examples — optional short task suggestions that MCP clients discover via `prompts/list`. For identity bindings, each example is automatically prefixed with the owner's name so callers address the right person (e.g., "ask John Doe (john@example.com) to generate employee report").

See **[Prompt Examples](../app_mcp_server/prompt_examples.md)** for full details on the concept, validation rules, prefixing behavior, and user flows.

## Integration Points

- **[App MCP Server](../app_mcp_server/app_mcp_server.md)** — `AppMCPRoutingService.route_message()` composes two candidate providers for Stage 1: `ChannelCandidateProvider` (the caller's own eligible agents), and `IdentityCandidateProvider` (one candidate per identity owner, with an `identity:{owner_id}` ref so a person can never be mistaken for an agent). When an identity candidate wins, Stage 2 routing runs
- **[Server Channels](../server_channels/server_channels.md)** — the second consumer, since Phase 3 of the channels & identity unification. `ChannelRoutingService._route_installed` appends `IdentityCandidateProvider.build(...)` to the sender's own-agent candidates **only when `policy.allow_identity_routing` is on**, and `ChannelRoutingService.decide` calls Stage 2 itself when a person wins. What crosses back is an `IdentityGrant`, re-verified in full at ingest. The sender opts in per channel from Settings → Channels; the per-person contact toggle is the *same* `IdentityBindingAssignment.is_enabled` switch App MCP uses, reused rather than duplicated per channel
- **[Auto Routing Tuning](../routing_tuning/routing_tuning.md)** — since Phase 3 the channel Pass-1 capture is open around this feature's Stage-1 candidates *and* around Stage 2 (recorded under the `identity_stage2` stage), so identity routing finally writes durable trace rows. An identity owner with nothing currently reachable is recorded as a `SKIP_IDENTITY_UNAVAILABLE` skip; an owner the sender could have reached with the channel switch **off** is deliberately not recorded at all (see Business Rules below)
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — identity sessions use the same `Session` model with three additional columns: `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`. `integration_type` is `"identity_mcp"` on the App MCP path; on the channel path it stays `"channel_<type>"`, which is load-bearing — `ChannelOutboundService._resolve_channel_session` gates reply delivery on that prefix, so a session stamped `identity_mcp` there would route correctly, run correctly, and never deliver a word
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — uses the same shared OAuth AS and App MCP token infrastructure; no separate OAuth flow
- **[Agent Management](../agent_management/agent_management.md)** — as of Phase 5, identity registration no longer lives on the agent's Integrations tab MCP Connectors card at all (that dialog dropped both its "App MCP Server Integration" and "Identity MCP Server Integration" options, leaving only Direct MCP Connector and, for developers, Agent to Agent MCP Connector). Identity binding creation is Settings → Channels → Identity Server card only

## Access Control

| Action | Who |
|--------|-----|
| Create/edit/delete identity agent bindings | Agent owner only (binding.owner_id == current_user.id) |
| Assign users to a binding | Binding owner only |
| Set `auto_enable = True` on assignment | Superuser only |
| Enable/disable received identity contact | Target user (per-person toggle on their own assignments) |
| View own identity bindings and assignments | Identity owner only |
| View received identity contacts | Target user only |
| Route messages to an identity (App MCP) | Any user with at least one active, enabled binding assignment |
| Route messages to an identity (Server Channels) | The same, **plus** the sender's own `allow_identity_routing` opt-in on that channel — off by default, never inheritable from an admin channel default |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No accessible bindings for caller | Stage 2 returns error: routing fails gracefully |
| Stage 2 AI cannot determine agent | Error: "Could not determine which of {owner_name}'s agents to use. Please be more specific." |
| Binding disabled mid-conversation | Next message returns: "This identity connection is no longer active." |
| Assignment disabled or removed mid-conversation | Same: "This identity connection is no longer active." |
| Agent deleted (binding cascade) | Binding and all assignments are deleted; the caller's identity candidates no longer include this person if no other bindings remain |
| Self-assignment attempt | Silently skipped (self-exclusion) |
| Non-superuser sets `auto_enable = True` | 403: "Only administrators can auto-enable identities for users" |
| Duplicate binding (`owner_id, agent_id`) | 409: "Agent already added to identity" |
| Agent not owned by user | 403: "You can only add your own agents to your identity" |
| Cross-caller context_id use | Falls through to new routing (security; `identity_caller_id` must match) |
| Stage 2 selects nothing (channel path) | Ordinary `no_match` for the whole decision — the sender gets the generic "couldn't find an agent" reply, and Pass 2 auto-install does not run |
| Stage 2 raises (LLM outage, channel path) | Recorded on the trace and declined; it never 500s the externally-triggerable webhook |
| Grant revoked mid-thread (channel path) | The next message is refused by `assert_access`; the sender sees the same generic failure reply every other refusal gets, the binding fails, and the following message re-routes over the sender's own agents |
| Sender turns `allow_identity_routing` off mid-thread | Same generic decline on the next message, with the same self-healing binding — a bound identity thread does not survive the withdrawal of the consent that opened it |
| Stage 2 returns an agent whose owner is not the person Stage 1 chose | Rejected (`SKIP_FOREIGN_OWNER`), unreachable by construction — a binding may only name its owner's own agent |
