# App MCP Server

## Purpose

A universal, application-level MCP endpoint that any authenticated platform user can connect to. Unlike per-agent MCP connectors (which expose a single agent), the App MCP Server acts as a **router**: it receives a message, determines which agent should handle it, creates a session with that agent, and streams the response back — all transparently to the MCP client.

Users connect once with a single URL and interact with multiple agents through natural language, without needing to configure anything per agent.

**As of Phase 5 of the channels & identity unification refactor, App MCP is a [Server Channel](../server_channels/server_channels.md)** — a singleton `ServerChannel` row (`channel_type="app_mcp"`) resolved by the same `ChannelPolicyService` every other channel uses. The entire `AppAgentRoute` family (admin routes, per-user assignments, personal routes, `is_auto_managed`, backfill-on-demand, `message_patterns`, the install-time conflict toast) is **deleted**. There is nothing left to create or assign: **App MCP routes over every agent the caller owns that has a router trigger prompt or example prompts, plus the identities they have enabled** — the identical candidate set [Server Channels](../server_channels/server_channels.md) routes over for the same person.

## Core Concepts

| Term | Definition |
|------|-----------|
| **App MCP Channel** | The singleton `ServerChannel` row (`channel_type="app_mcp"`) that carries App MCP's admin kill switch, `visibility` + grant allowlist, per-user agent scope, and `allow_identity_routing`. Created lazily on first use by `ServerChannelService.get_or_create_singleton`; at most one may ever exist. See [Server Channels — App MCP as a channel](../server_channels/server_channels.md#app-mcp-as-a-channel) |
| **Authenticated transport** | The `ChannelCapabilities.inbound_mode` App MCP declares: `needs_webhook_token=False`, `needs_outbound_credentials=False`. There is no external sender to whitelist and no request to verify — the caller already holds a platform bearer token bound to one `user_id` |
| **Router Trigger Prompt** | `Agent.router_trigger_prompt` — the short natural-language sentence an owner writes describing when their agent should be picked. The sole trigger-side routing input, alongside example prompts |
| **Example Prompts** | `Agent.example_prompts` — a list of ready-to-send task suggestions on the agent itself, edited from the Configuration tab. Both fed to the classifier and surfaced as MCP prompts |
| **Candidate Providers** | The two composable building blocks every routing surface shares: `ChannelCandidateProvider` (the caller's own agents, narrowed to the resolved agent scope) and `IdentityCandidateProvider` (the people the caller can address, one candidate per identity owner). App MCP composes them in that order — owned agents first, identities after — exactly as [Server Channels](../server_channels/server_channels.md) does |
| **Routing Result** | Output of the routing engine: which agent (or which identity, handed to Stage 2) was selected and how (`only_one` or `ai`) |
| **Revocation delay** | The App MCP availability cache TTL (`settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS`, default **45 seconds**). An admin disabling the channel, withdrawing a grant, or the caller's own toggle takes up to this long to actually refuse the next MCP call — see [Availability is checked at token use, not at issue](#availability-is-checked-at-token-use-not-at-issue) |

## User Stories / Flows

### Any Agent Owner: Become Reachable Over App MCP

There is no dialog to fill in and nothing to create. An agent becomes an App MCP candidate the moment it has a router trigger prompt or at least one example prompt:

1. Owner opens their agent's **Configuration** tab.
2. Sets a **Router Trigger Prompt** — one sentence describing when the agent should be picked (e.g. "Handles annual report generation and financial summaries") — and/or adds **Example Prompts**.
3. As soon as it is saved, the agent is reachable from every MCP client connected to the caller's own App MCP session, subject to the caller's own agent scope on the channel (see [Server Channels — Agent scope](../server_channels/server_channels.md#agent-scope)).

The agent's Integrations tab **MCP Connectors card** no longer offers an "App MCP Server Integration" option — the "New" dialog now has exactly two choices, **Direct MCP Connector** and (for developers) **Agent to Agent MCP Connector**. See [Agent Management](../agent_management/agent_management.md#mcp-connectors).

For the `agent-user` role, `McpConnectorsCardSimple` on the Integrations tab is now purely explanatory: it mirrors the agent's trigger prompt read-only and states that the agent is reachable — there is no per-agent enable/disable switch any more, because there is nothing to switch. Reachability is governed entirely by the App MCP channel row (Settings → Channels) and the trigger prompt (Configuration tab).

### User: Connect via App MCP

1. User opens **Settings → Channels** and finds the **MCP Server** card.
2. Copies the MCP Server URL displayed in the card header (or clicks the `?` help button for step-by-step instructions via the Getting Started modal).
3. Pastes the URL into an MCP client (Claude Desktop, Cursor, etc.) as a new MCP connector.
4. Completes the OAuth flow.
5. User types a message; the MCP client calls the `send_message` tool.
6. App MCP routes the message to the right agent (or identity), creates or reuses a session, and streams the response back.
7. Subsequent messages in the same chat reuse the session (via `context_id`).

The **App MCP Server** row itself lives among the other channels on the same Settings → Channels page: the caller's own on/off toggle, agent scope, and `allow_identity_routing` switch are set there, exactly as for Google Chat or Email. See [Server Channels — Availability and per-user settings](../server_channels/server_channels.md#availability-and-per-user-settings).

### User: Address a Colleague from an MCP Client

Composing the identity candidate provider means an MCP client can reach another person's agents the same way a Google Chat message can — see [Identity MCP Server](../identity_routing/identity_routing.md). This requires the caller to have turned `allow_identity_routing` on for the App MCP channel in Settings → Channels; **it is off by default and does not inherit an admin default** (see [Business Rules](#business-rules) below for why every existing user is affected).

### Superuser: Configure App MCP as a Channel

App MCP now has an admin-facing side it never had before, on the **Channels** tab of `/admin/server-configuration`, alongside every other channel: a kill switch (`enabled`), `visibility` (`public` or `restricted` + a grant allowlist), and a default agent scope. The admin form renders no webhook, secret, or whitelist field for this row — the app-MCP adapter declares an `authenticated` transport with nothing of the sort, and the form drives off that declaration rather than a `channel_type` check. See [Server Channels — App MCP as a channel](../server_channels/server_channels.md#app-mcp-as-a-channel).

## Business Rules

### Composed Routing, Not Route Lookup

The Stage 1 ballot is composed from exactly the same two providers [Server Channels](../server_channels/server_channels.md) uses, in the same order:

1. `ChannelCandidateProvider.build(db, user_id, policy=policy)` — every agent the caller owns, narrowed to the resolved agent scope (`"all"` / `"list"` / `"none"`), eligible when it has a non-blank `router_trigger_prompt` or non-empty `example_prompts`. An agent excluded by scope or wording is recorded as a skipped candidate, never silently dropped.
2. `IdentityCandidateProvider.build(db, user_id, policy=policy)` — one candidate per identity owner the caller may currently address. **`policy` is required and keyword-only**, and the consent gate lives *inside* `build`: with `policy.allow_identity_routing` off it returns `[]` immediately, before any query and without writing a trace row of any kind. The provider is therefore always called; what changes is what it returns. (Until Phase 7 of the channels & identity unification the gate was an `if` copied out at each of the three call sites — do not re-add one. The observable outcome is unchanged: no identity candidate, and no identity trace row.) See [Identity MCP Server](../identity_routing/identity_routing.md).

Neither surface borrows the other's candidate set or enablement toggles; they compose the same building blocks independently. A standalone (non-bundle) agent needs nothing special any more — the old motivating bug ("a standalone agent has no `AppAgentRoute` by construction and is invisible to routing") no longer exists, because there is no route in the story at all.

### ⚠️ Identity Routing Is Opt-In and Does Not Inherit

**`allow_identity_routing` defaults to `false` and never inherits from a channel default.** It is the **sender's own** consent that a message of theirs may open a session inside somebody else's workspace, where that person can read it — not the receiver's control over who reaches them (that is the identity owner's bindings and per-person assignments). An admin default must therefore not be able to switch it on for someone who never agreed, which is why it is `NOT NULL DEFAULT false` with no channel-level default to inherit; see `ChannelUserSetting`'s module docstring for the full semantics. **Scope of the switch:** it governs identity routing on **channels and App MCP** only. **External A2A identity access is authorized by a separate mechanism** — `ExternalAccessPolicy.require_identity_access` plus the identity owner's binding assignments — and is **not** closed by this switch. This is a **deliberate behaviour change**, not an oversight, and its effect is immediate on every existing deployment: **every App MCP user who could previously reach an identity contact loses that reach until they turn the switch on themselves**, from **Settings → Channels → App MCP Server → Identity routing**. There is no admin override that can restore it on their behalf.

### Availability Is Checked at Token Use, Not at Issue

`app_mcp_token` rows are unaffected by any of this — a minted token is not revoked. Instead, `AppMCPTokenVerifier` checks, on every request, whether the App MCP channel is currently available to the token's owner: `ServerChannel.enabled` AND access (public, or a granted `restricted` channel) AND the caller's own per-channel toggle, all resolved through `ChannelPolicyService.resolve(...).is_available` — the identical conjunction every other channel is gated on.

That resolution is expensive to run on every MCP call (`tools/call`, `tools/list`, `prompts/list`, every SSE reconnect), so it is cached **per user id, in process memory, for `settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS` (default 45 seconds)**. Concretely:

- **An admin disabling the channel, withdrawing a grant, or the caller's own toggle takes up to 45 seconds to actually refuse the next call** — the documented revocation delay.
- The cache **fails closed**: a lookup that raises an exception denies the caller and is never cached in either direction — a transient database blip costs one denial, not a TTL-long one, and it can never be mistaken for "available" because of an outage.
- A `TTL <= 0` bypasses the cache entirely — useful for a test or an operator who wants the switch to bite immediately, at the cost of the extra database read on every call.

An invalid token and a channel switched off for a valid one return the identical, undistinguishable refusal — telling them apart would be an oracle for a server's channel configuration.

### Message Transformation

When the AI router classifies a message, it also **strips routing prefixes** and extracts the core task, so agents receive clean, actionable messages instead of delegation phrasing.

- The AI router returns both a selected candidate and a transformed message.
- Routing prefixes like "ask cinna to...", "tell john to...", "forward to X..." are stripped automatically.
- If the message has no routing prefix (it's already a direct task), no transformation occurs.
- The single-candidate shortcut delivers the original message unchanged (no AI involved).

**Examples:**
- "ask cinna to generate employee report" → agent receives "generate employee report"
- "tell john to fix the bug" → agent receives "fix the bug"
- "generate report" → agent receives "generate report" (no prefix, no change)

**Two-level transformation (through Identity):**
- "ask cinna to ask john to generate report" → Stage 1 strips one layer → "ask john to generate report" → Stage 2 strips another → agent receives "generate report"

**Cascade logic:**
- If both stages transform, the final (Stage 2) transformation is used.
- If only Stage 1 transforms, its output is used.
- If neither transforms, the original message is used.

**Safety guards:**
- Empty or whitespace-only transformations are discarded (original used).
- Transformations identical to the original are discarded.
- Transformations exceeding 2x the original message length are discarded (prevents hallucinated expansions).

**Auditability:** when a transformation occurs, the original message is stored in `session_metadata["app_mcp_original_message"]` for traceability.

### Routing Priority

1. **Single-candidate shortcut** — if the whole ballot (owned agents + identities, when enabled) holds exactly one entry, use it directly; no classification call is made.
2. **AI classification** — the shared `AgentClassifier.classify` (the same classifier every routing consumer uses since [Auto Routing Tuning](../routing_tuning/routing_tuning.md)'s Phase 5) is called with the message and the whole ballot; each candidate is passed as `{id, name, trigger_prompt, prompt_examples}`.
3. **No match** — an error asking the caller to be more specific.

When the winner is an identity candidate, Stage 2 (`IdentityRoutingService.route_within_identity`) picks the agent from that person's portfolio and returns the binding + assignment ids that become the `IdentityGrant` re-verified at ingest — see [Identity MCP Server](../identity_routing/identity_routing.md).

Glob/pattern pre-matching is gone entirely (`message_patterns` is dropped everywhere, settled decision §2.9 of the master plan): it was a second, silently-higher-priority routing mechanism no trace explained well.

### Session Management

- First message creates a new session with `integration_type = "app_mcp"` (or `"identity_mcp"` when the winner was an identity).
- Response includes `context_id` (the session UUID).
- Subsequent messages with the same `context_id` reuse the existing session (no re-routing).
- `context_id` is validated: session must have `integration_type = "app_mcp"` and `caller_id` matching the authenticated user.
- Invalid or missing `context_id` triggers a new routing + session creation.
- Sessions are independent of any per-agent configuration once created.

### Session Ownership

App MCP sessions are owned by the **agent owner**, not the MCP caller:

| Field | Value | Description |
|-------|-------|-------------|
| `user_id` | `agent.owner_id` | The agent owner — they see and manage the session in the platform UI |
| `caller_id` | caller's user ID | The MCP client user who initiated the session — tracked for audit and display |

This means:
- The **agent owner** sees all App MCP sessions in their Sessions list.
- The **caller** does not see the session in their own platform UI (they interact through their MCP client only).
- Session page shows a "MCP" badge and a caller email badge so the owner knows who initiated the session.
- `caller_id` is set to `NULL` on `ON DELETE SET NULL` if the caller's account is deleted; the session remains visible to the owner.
- The caller can still resume their session using the `context_id` returned from the first message.

### Access Control on the Underlying Session

`ChannelIngestionService.assert_access`'s `mcp_caller` arm used to trust "the App MCP routing layer verified the caller has a route to this agent." With no route left to have verified that, the arm now requires that **the agent be owned by the caller, or an identity grant authorizes it** — reusing the same `IdentityService.verify_identity_access` six-condition check every other surface's identity arm uses, rather than inventing a second one. See [Identity MCP Server — Business Rules](../identity_routing/identity_routing.md#per-message-re-verification-channel-path).

### OAuth / Authentication

- Reuses the existing shared OAuth AS at `/mcp/oauth/...`.
- MCP clients register via the same DCR endpoint with `resource` pointing to `/app/mcp`.
- Consent page shows "Application MCP Server" instead of a specific agent name.
- Any authenticated platform user can approve (no email ACL needed — the user IS the ACL), subject to the App MCP channel's own `visibility` when restricted.
- Tokens are app-scoped (stored in `app_mcp_token`), not connector-scoped, and are never revoked by a channel change — see [Availability Is Checked at Token Use, Not at Issue](#availability-is-checked-at-token-use-not-at-issue).

## Architecture Overview

```
MCP Client (Claude Desktop, Cursor, etc.)
    |
    |  OAuth 2.1 (shared AS at /mcp/oauth/...)
    v
App MCP Server (/mcp/app/mcp)
    |
    +-- AppMCPTokenVerifier: is the token valid, AND is the App MCP channel
    |   available to this user right now (ChannelPolicyService, 45s cache,
    |   fail-closed)?
    +-- Receive send_message(message, context_id)
    |
    +-- 1. If context_id exists -> reuse existing session
    |
    +-- 2. AppMCPRoutingService.route_message:
    |      - ChannelCandidateProvider (owned agents, agent-scope narrowed)
    |      - + IdentityCandidateProvider (returns [] unless the caller's own
    |        allow_identity_routing is on — gate is inside build)
    |      - single-candidate shortcut, else AgentClassifier.classify
    |      - a person wins -> Stage 2 (IdentityRoutingService) picks the agent
    |
    +-- 3. Create session with selected agent, send transformed message,
    |      stream response
    |
    +-- Return { response, context_id, agent_name }

Agent Owner UI:  Agent > Configuration tab > Router Trigger Prompt / Example Prompts
User Settings:   Settings > Channels > "App MCP Server" row (on/off, scope,
                 identity routing) + "MCP Server" card (URL + connect help)
Admin UI:        Admin > Server Configuration > Channels > App MCP Server row
                 (kill switch, visibility + grants, default agent scope)
```

### Integration with Per-Agent MCP

| Component | Per-Agent MCP | App MCP Server |
|-----------|--------------|----------------|
| OAuth AS | Shared `/mcp/oauth/...` | Same shared AS |
| Resource Server URL | `/mcp/{connector_id}/mcp` | `/mcp/app/mcp` |
| Token Verifier | `MCPTokenVerifier(connector_id)` | `AppMCPTokenVerifier()` |
| MCPServer instance | One per connector (lazy) | Single instance |
| Session routing | `context_id` → fixed agent | `context_id` → routed agent |
| Registry | `MCPServerRegistry` dispatches by connector UUID | Special `"app"` path handled before UUID validation |

## Integration Points

- **[Server Channels](../server_channels/server_channels.md)** — App MCP is one `ServerChannel` row (`channel_type="app_mcp"`, the platform's `authenticated` transport). It shares the admin kill switch, `visibility` + grants, per-user agent scope, and `allow_identity_routing` model with every other channel, resolved by the same `ChannelPolicyService`
- **[Identity MCP Server](../identity_routing/identity_routing.md)** — `IdentityCandidateProvider` supplies the people this caller can address; Stage 2 picks the agent when a person wins
- **[Auto Routing Tuning](../routing_tuning/routing_tuning.md)** — **since Phase 6 of the channels & identity unification, App MCP writes routing traces of its own.** `AppMCPRoutingService.route_message` opens the capture with `origin="app_mcp"` and the singleton channel's `channel_id`, so every candidate, skip reason and identity Stage-2 handoff on this surface is durably recorded and diagnosable from `/admin/routing-tuning` — the recorder calls in that service existed long before and were no-ops for want of an open capture. How much of each row is written is governed by `ROUTING_TRACE_APP_MCP_MODE` (`off` | `metadata` | `full`), **defaulting to `metadata`** because App MCP routes *every* message rather than only thread openings and sits behind no webhook rate limit: it is the one origin whose write volume is unbounded. App MCP still emits **no** [Channel Debug Monitor](../server_channels/channel_debug_monitor.md) events — its reply is the synchronous MCP response, so there is no outbound delivery to hook
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — reuses the shared OAuth AS, MCPServerRegistry, and session infrastructure
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — App MCP sessions use the same Session model with `integration_type = "app_mcp"` and `agent_id` for direct agent resolution
- **[AI Functions](../../development/backend/ai_functions_development.md)** — the AI router classifies via `AgentClassifier.classify()` (`backend/app/services/routing/agent_classifier.py`), the classifier shared by every routing consumer since [Auto Routing Tuning](../routing_tuning/routing_tuning.md)'s Phase 5. The underlying provider cascade still defaults to `gemini-2.5-flash-lite`
- **[Agent Management](../agent_management/agent_management.md)** — reachability is driven entirely by `Agent.router_trigger_prompt` and `Agent.example_prompts`, both edited from the Configuration tab; there is no App MCP-specific creation flow left on the Integrations tab
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — native clients (Cinna Desktop) discover the same personal-agents + identity-contacts sections through `GET /external/agents`; the old "MCP shared routes" middle section is gone there too

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No candidates for user (no owned agent has wording, and no identity enabled) | Error: "No agents are configured for your account. Contact your admin." |
| AI router can't determine agent | Error: "Could not determine which agent to use. Please be more specific." |
| App MCP channel disabled, or caller not granted/enabled for it | Token refused with the same generic 401 an invalid token gets, within the 45s cache TTL |
| Agent environment not active | Environment auto-activates; pending until ready |
| Agent deleted after session created | Error: "The agent for this conversation is no longer available" |
| Cross-caller `context_id` (caller B using caller A's context_id) | `caller_id` mismatch; session not found; falls through to new routing |
| Concurrent messages on same session | Error: "Another message is being processed. Please wait." |

## MCP Prompts

The App MCP Server exposes the caller's routable targets as MCP prompts via `prompts/list`, mirroring exactly what Stage 1's ballot would contain: the caller's own eligible agents, plus (when `allow_identity_routing` is on) the identity owners they can address. See [Prompt Examples](prompt_examples.md) for the full field-level behavior.

## Deprecation Notes

### The `AppAgentRoute` Family — Deleted

`AppAgentRoute`, `AppAgentRouteAssignment`, and `UserAppAgentRoute` (and everything built on them — admin-managed routes, per-user route assignments, the legacy personal routes, `is_auto_managed`, backfill-on-demand, the install-time conflict toast, install-time auto-route creation) are deleted outright, with no data migration (settled decision §2.8 of the master plan: "Admin makes their agent addressable by user B" is now served by identity or by an admin-published bundle, not by a route). Existing sessions created through the old route path continue to work by their `context_id`; there is nothing left in the platform that creates, assigns, or displays a route.

### The Admin "Application Agents" Page — Already Removed

The dedicated admin "Application Agents" page and its sidebar menu item were removed before this phase and remain removed; there is no route-specific admin surface at all any more — App MCP's admin controls now live on the **Channels** tab of `/admin/server-configuration`, alongside every other channel.
