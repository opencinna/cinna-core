# App MCP Server -- Technical Details

**As of Phase 5 of the channels & identity unification refactor**, App MCP is a `ServerChannel` row (`channel_type="app_mcp"`) and the entire `AppAgentRoute` family is deleted. This doc describes the current state only; see [App MCP Server](app_mcp_server.md) for the business-logic side of the same change.

## File Locations

### Backend -- Models

- `backend/app/models/server_channels/server_channel.py` -- `ServerChannel`, `ServerChannelBase`, `ServerChannelCreate/Update/Public`, the `CHANNEL_VISIBILITY_*` / `CHANNEL_AGENT_SCOPE_*` constants — App MCP is one row here, `channel_type="app_mcp"`, enforced singleton by a partial unique index (`uq_server_channel_singleton_type`, migration `867cacb5a827`) and by `ServerChannelService` refusing a second one
- `backend/app/models/app_mcp/app_mcp_token.py` -- `AppMCPToken` (opaque OAuth tokens for app-level MCP) — unaffected by this phase; availability is checked at use, not by revoking tokens
- `backend/app/models/app_mcp/app_mcp_oauth_client.py` -- `AppMCPOAuthClient` (DCR clients for app-level MCP)
- `backend/app/models/app_mcp/app_mcp_auth_code.py` -- `AppMCPAuthCode`, `AppMCPAuthRequest` (OAuth authorization flow)
- `backend/app/models/app_mcp/__init__.py` -- re-exports the remaining app_mcp models (the route DTOs are gone)
- `backend/app/models/__init__.py` -- includes app_mcp and server_channels models in the global re-export

**Deleted in this phase:** `backend/app/models/app_mcp/app_agent_route.py` — `AppAgentRoute`, `AppAgentRouteAssignment`, `UserAppAgentRoute` and every DTO built on them. <!-- nocheck -->

### Backend -- Routes

- `backend/app/api/routes/server_channels.py` -- admin CRUD for `ServerChannel` rows, App MCP included; the admin form's field set is driven by the transport's declared `ChannelCapabilities`, not a `channel_type` branch
- `backend/app/api/routes/agents.py` -- `PATCH /agents/{id}/router-trigger-prompt` (unchanged endpoint; no longer calls anything that syncs a route)
- `backend/app/api/routes/utils.py` -- `GET /api/v1/utils/mcp-info/` returns `{ mcp_server_url }`

**Deleted in this phase:** `backend/app/api/routes/agent_app_mcp_routes.py`, `backend/app/api/routes/app_agent_routes.py`, `backend/app/api/routes/user_app_agent_routes.py`, and their registrations in `backend/app/api/main.py`. <!-- nocheck -->

### Backend -- Services

- `backend/app/services/routing/channel_candidate_provider.py` -- `ChannelCandidateProvider.build(db, user_id, policy=policy)` — the caller's own eligible agents, narrowed to the resolved agent scope. Shared verbatim with [Server Channels](../server_channels/server_channels_tech.md); App MCP's `route_message` calls it directly
- `backend/app/services/routing/identity_candidate_provider.py` -- `IdentityCandidateProvider.build(db, caller_user_id, *, policy)` -- the identity half of Stage 1's ballot, one `Candidate` per identity owner. `policy` is **required and keyword-only** since Phase 7 of the channels & identity unification, and `build` itself returns `[]` when `policy.allow_identity_routing` is off — the consent gate is inside the provider, not at its call sites; `identity_ref_id()` / `parse_identity_ref()` own the `identity:{owner_id}` `ref_id` namespace
- `backend/app/services/app_mcp/app_mcp_routing_service.py` -- `AppMCPRoutingService.route_message(db_session, user_id, message)`: resolves the App MCP channel + policy (`ServerChannelService.get_or_create_singleton` + `ChannelPolicyService.resolve`), composes `ChannelCandidateProvider` with `IdentityCandidateProvider` (always called, handed the resolved `policy`; it returns `[]` when `allow_identity_routing` is off), single-candidate shortcut or `_ai_classify`, hands off to Stage 2 via `_route_identity` when a person wins. `IdentityPick` is Stage 1's answer shape when a person wins; `RoutingResult.source` is `"owned"` or `"identity"` (replacing the old `route_source` "admin"/"user"/"identity"). No `_try_pattern_match` any more — `message_patterns` is gone
- `backend/app/services/app_mcp/app_mcp_request_handler.py` -- `AppMCPRequestHandler` with `handle_send_message()`, `_resolve_session()`, session lock management; uses `routing_result.transformed_message` as `effective_message` for message creation and title generation; stores `app_mcp_original_message` in session metadata when transformation occurs
- `backend/app/services/app_mcp/app_mcp_oauth_service.py` -- `AppMCPOAuthService` for app-level OAuth token lifecycle
- `backend/app/services/server_channels/adapters/app_mcp.py` -- `AppMCPChannelAdapter`: the transport declaration only (`inbound_mode="authenticated"`, `needs_webhook_token=False`, `needs_outbound_credentials=False`, `is_singleton=True`); `validate_config` rejects any non-empty config; `has_outbound_credentials` always returns `True` (there is nothing to be missing); `get_setup_instructions` renders the admin panel's "already connected" copy, including the revocation-delay sentence
- `backend/app/services/server_channels/channel_policy_service.py` -- `ChannelPolicyService.resolve(db, channel, user_id) -> ResolvedChannelPolicy` — the single place App MCP's availability, agent scope, and `allow_identity_routing` are resolved, shared with every other channel
- `backend/app/services/server_channels/server_channel_service.py` -- `ServerChannelService.get_or_create_singleton(db, channel_type)` — the lazy-materialization accessor App MCP's routing service, token verifier, and prompt listing all share

**Deleted in this phase:** `backend/app/services/app_mcp/app_agent_route_service.py` (`AppAgentRouteService`, `UserAppAgentRouteService`, `get_effective_routes_for_user`), and `AppMCPRoutingService._try_pattern_match`. <!-- nocheck -->

### Backend -- MCP Server

- `backend/app/mcp/app_server.py` -- `create_app_mcp_server()` singleton factory
- `backend/app/mcp/app_tools.py` -- `send_message` tool registration on the App MCP FastMCP instance
- `backend/app/mcp/app_prompts.py` -- dynamic per-user MCP prompt listing; resolves the channel + policy, composes `ChannelCandidateProvider` with `IdentityCandidateProvider` (gated on `allow_identity_routing`) — the identical composition `route_message` uses, so the discovery list matches Stage 1's ballot exactly
- `backend/app/mcp/app_token_verifier.py` -- `AppMCPTokenVerifier`: token validity first (hashed lookup, expiry, revocation), then `is_app_mcp_available(user_id)` — see [Availability cache](#availability-cache-app_token_verifierpy) below
- `backend/app/mcp/server.py` -- `MCPServerRegistry` extended with `"app"` path handling and `get_or_create_app_server()`

### Backend -- AI Router

- `backend/app/agents/app_agent_router.py` -- since [Auto Routing Tuning](../routing_tuning/routing_tuning.md)'s Phase 5, a thin `list[dict]`-in adapter over `backend/app/services/routing/agent_classifier.py`'s `AgentClassifier.classify` — see that feature's tech doc for the actual prompt rendering, parsing, and trace emission. `RouteToAgentResult` is an alias of `ClassificationResult`; `route_to_agent(message, available_agents, provider_kwargs=None)` still takes `available_agents` as `{id, name, trigger_prompt, prompt_examples}` dicts and returns both agent ID and optional transformed message; validates transformation (discards empty, identical, or >2x-length results)
- `backend/app/agents/router_trigger_prompt_generator.py` -- `generate_router_trigger_prompt(agent_name, description, provider_kwargs)`; model `gemini-2.5-flash-lite`; target output ~120–150 chars; on any generation failure returns a fallback string
- `backend/app/agents/prompts/app_agent_router_prompt.md` -- prompt template instructing the LLM to return JSON `{"agent_id": "...", "message": "...", "confidence": 0.0, "reason": "...", "runner_up": "<uuid>|NONE"}`, with an "Example messages" sub-list per candidate rendered from `prompt_examples`. `confidence` / `reason` / `runner_up` are advisory only — see [Auto Routing Tuning](../routing_tuning/routing_tuning.md#phase-5--classifier-unification-prompt_examples-and-confidence)
- `backend/app/services/ai_functions/ai_functions_service.py` -- `route_to_agent()` delegates to the adapter above; `generate_router_trigger_prompt(agent_name, description, user, db)` wraps the generator

### Backend -- OAuth Extensions

- `backend/app/mcp/oauth_routes.py` -- extended for app-level resource URLs (register, authorize, token, revoke)
- `backend/app/api/routes/mcp_consent.py` -- extended GET/POST to handle `AppMCPAuthRequest` nonces

### Backend -- Migrations

- `backend/app/alembic/versions/867cacb5a827_remove_app_agent_routes_and_enforce_.py` -- **this phase's migration.** Drops `app_agent_route_assignment`, `app_agent_route`, `user_app_agent_route` (in FK-safe order); drops `identity_agent_binding.message_patterns`; adds the partial unique index `uq_server_channel_singleton_type` on `server_channel.channel_type` restricted to `IN ('app_mcp')`
- `backend/app/alembic/versions/72406c16543f_add_agent_id_to_session_table.py` -- adds `agent_id` to `session` table with backfill
- `backend/app/alembic/versions/2c222ba66e57_add_caller_id_to_session.py` -- adds `caller_id` column + FK + index

**Deleted in this phase:** `backend/app/scripts/backfill_router_trigger_prompts.py` (the Phase-8 backfill script from the earlier App MCP work). <!-- nocheck -->

### Frontend

- `frontend/src/components/Agents/McpConnectorsCard.tsx` -- "New" dialog now offers exactly two options: **Direct MCP Connector** and, for developers, **Agent to Agent MCP Connector**. "App MCP Server Integration" and "Identity MCP Server Integration" are gone from the type-select step
- `frontend/src/components/Agents/McpConnectorsCardSimple.tsx` -- purely explanatory for `agent-user`: takes `routerTriggerPrompt` as a prop (no longer fetches `app-mcp-routes`), renders a read-only mirror of the trigger prompt, and states the agent is reachable — no per-user toggle, since there is nothing left to toggle per agent
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` -- passes `agent.router_trigger_prompt` into `McpConnectorsCardSimple`
- `frontend/src/components/Agents/EditRouterTriggerPromptModal.tsx` -- no longer invalidates an `["app-mcp-routes", agentId]` query key (nothing reads it any more) and drops the copy promising a route "will appear here automatically"
- `frontend/src/components/UserSettings/AppMcpServerCard.tsx` -- stripped to the **MCP Server URL** (copyable) + connect-instructions button only, and renamed from `AppAgentRoutesCard.tsx` in Phase 7 of the channels & identity unification, since there are no routes left for it to be named after. Everything else — "MCP Shared Agents", "Personal Routes", per-route toggles — is gone; `UserChannelsCard`'s App MCP row now owns the on/off + agent-scope + identity-routing controls
- `frontend/src/components/UserSettings/UserChannelsCard.tsx` -- renders the App MCP channel row like any other channel; the identity-contacts toggle list this card fetches is now the **sole** surface for that list (previously duplicated on `AppAgentRoutesCard`, renamed `AppMcpServerCard.tsx` in Phase 7)
- `frontend/src/components/Onboarding/GettingStartedModal.tsx` -- "After Connecting" article rewritten: routing is by trigger prompt, not "configured agent routes"; points at the App MCP Server row under Settings → Channels for the on/off + scope controls
- `frontend/src/routes/oauth/mcp-consent.tsx` -- adapted for `app_mcp=true` query param (unchanged by this phase)

**Deleted in this phase:** nothing frontend-side was deleted outright — `McpConnectorsCard.tsx` and `AppAgentRoutesCard.tsx` were stripped down rather than removed, since both still carry live functionality (direct/agent-to-agent connectors, and the MCP Server URL respectively) — the latter renamed to `AppMcpServerCard.tsx` in Phase 7.

### Tests

- `backend/tests/api/app_mcp/conftest.py` -- domain fixtures (patched create_session, environment adapter, background tasks)
- `backend/tests/api/app_mcp/app_mcp_session_test.py` -- session creation, context_id reuse, invalid context_id, no candidates, two independent sessions; session ownership (user_id=owner, caller_id=caller); cross-caller context_id isolation; caller_email in GET /sessions/{id} response
- `backend/tests/api/app_mcp/prompt_examples_test.py` -- `prompt_examples` lifecycle for `IdentityAgentBinding`; the `Agent.example_prompts` side is covered by the agent-prompts test group, not here
- `backend/tests/unit/test_routing_message_transformation.py` -- unit tests for `RouteToAgentResult`, JSON parsing, sanity guards, `_ai_classify()` tuple propagation, cascade logic, `AIFunctionsService` passthrough
- `backend/tests/architecture/` -- `AppAgentRoute` / `app_agent_route` / `UserAppAgentRoute` / `channel_app_mcp` are grep-checked as gone from `backend/app` outside alembic history

**Deleted in this phase:** `backend/tests/api/app_mcp/agent_app_mcp_routes_test.py`, `backend/tests/api/app_mcp/app_agent_routes_test.py`, `backend/tests/api/app_mcp/app_mcp_auto_managed_route_test.py`, `backend/tests/api/agents/bundles_install/agents_bundles_auto_mcp_route_test.py`, `backend/tests/api/agents/integrations/agents_backfill_router_trigger_test.py`, `backend/tests/api/external/external_a2a_route_test.py`, `backend/tests/utils/app_agent_route.py`. `backend/tests/unit/test_app_agent_route_similarity.py` was rewritten against the extracted `app/services/routing/text_similarity.py` module rather than deleted — the Jaccard helpers survive. <!-- nocheck -->

## Database Schema

### `app_mcp_token` -- OAuth tokens for app-level MCP

- `id` (UUID, PK), `user_id` (FK > user), `client_id` (str, indexed), `token_hash` (str, unique indexed), `token_type` (str), `scope` (str, nullable), `expires_at` (datetime), `is_revoked` (bool), `created_at`
- **Unaffected by this phase.** Availability is checked live at use (see [Availability cache](#availability-cache-app_token_verifierpy)), not by revoking or filtering these rows

### `app_mcp_oauth_client` -- DCR clients for app-level MCP

- `id` (UUID, PK), `client_id` (str, unique indexed), `client_secret_hash` (str), `client_name` (str, nullable), `redirect_uris` (JSON), `created_at`

### `app_mcp_auth_code` / `app_mcp_auth_request` -- OAuth authorization flow

- Auth request stores nonce, client_id, redirect_uri, scope, state, user_id, expires_at
- Auth code stores code hash, client_id, redirect_uri, scope, user_id, expires_at, is_used

### `server_channel` row for App MCP

No dedicated table — one row of the shared `server_channel` table (`channel_type="app_mcp"`). See [Server Channels — tech](../server_channels/server_channels_tech.md#database-schema) for the full column set (`enabled`, `visibility`, `default_enabled_for_users`, `default_agent_scope`, `allow_auto_install`). App MCP's row always has `config={}`, `encrypted_secrets=NULL`, `email_whitelist=NULL`, `webhook_token=NULL` — enforced by the adapter's `validate_config` and by `AppMCPChannelAdapter.capabilities` declaring `needs_webhook_token=False` / `needs_outbound_credentials=False`.

`uq_server_channel_singleton_type` — a partial unique index on `server_channel.channel_type` restricted to `channel_type IN ('app_mcp')` — is the database-level half of the singleton guarantee; `ServerChannelService` is the application-level half. Both must stay in agreement (see the model's own docstring on why the predicate is a hardcoded SQL literal, not derived at runtime).

### Session model extension (unaffected by this phase)

- `session.agent_id` (UUID, FK > agent, nullable, indexed)
- `session.integration_type = "app_mcp"` -- identifies App MCP sessions (`"identity_mcp"` for identity-routed ones)
- `session.user_id` -- set to `agent.owner_id`
- `session.caller_id` (UUID, FK > user, nullable, indexed, ON DELETE SET NULL) -- the MCP client user who initiated the session
- `SessionPublic.caller_id`, `SessionPublicExtended.caller_name` / `caller_email`
- Routing metadata in `session.session_metadata` JSON: `app_mcp_source` (`"owned"` or `"identity"` — `routing_result.source`, replacing the old `app_mcp_route_type`), `app_mcp_agent_name`, `app_mcp_session_mode`, `app_mcp_match_method`, `app_mcp_original_message` (only when message transformation occurred). `app_mcp_route_id` no longer exists — there is no route to record

## API Endpoints

### Utility

- `GET /api/v1/utils/mcp-info/` -- Returns `{ mcp_server_url }` for the frontend's copyable App MCP URL

### Admin — App MCP as a channel

App MCP's admin surface is the shared `ServerChannel` admin API (`backend/app/api/routes/server_channels.py`), documented in full in [Server Channels — tech](../server_channels/server_channels_tech.md#api-endpoints). There is no App MCP-specific admin route.

**Deleted in this phase**, with nothing replacing them (there is no route resource left to CRUD):
- `/api/v1/agents/{agent_id}/app-mcp-routes/` (agent-scoped route CRUD)
- `/api/v1/admin/app-agent-routes/` (superuser cross-agent route CRUD)
- `/api/v1/users/me/app-agent-routes/` (personal + shared route listing, assignment toggle)

## Availability cache (`app_token_verifier.py`)

`is_app_mcp_available(user_id) -> bool` — the cached, fail-closed wrapper `AppMCPTokenVerifier.verify_token` calls after token validity passes.

- Cache keyed per `user_id`, held in process memory (`_availability_cache: dict[UUID, _AvailabilityEntry]`, guarded by `_availability_lock`), TTL from `settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS` (default **45**).
- `_resolve_availability` is three-valued: `True` / `False` are answers the database gave; `None` means the lookup itself failed (an exception was caught) and is **never cached** in either direction.
- `is_app_mcp_available` collapses `None` to `False` for the caller (denies) but only stores an actual `True`/`False` answer — so a transient failure costs exactly one denial, not a TTL-long one, and can never be cached as `True`.
- `ttl <= 0` bypasses the cache entirely, resolving fresh on every call — the deterministic path a test uses to observe a revocation without sleeping.
- `reset_availability_cache()` clears every entry — for process-lifecycle callers and tests, not part of the revocation mechanism itself (the TTL is).
- Eviction: when the cache hits `settings.APP_MCP_AVAILABILITY_CACHE_MAX_ENTRIES`, expired entries are dropped first; if that alone doesn't free room, the whole cache is cleared (never a wrong answer — every entry is re-derivable).

`AppMCPTokenVerifier.verify_token` order: token hash lookup → expiry → revocation → (only if all pass) `is_app_mcp_available(user_id)`. An invalid token never triggers a policy resolution, and an unverified token can never populate a cache entry keyed on a user id it never proved it owns.

## Services & Key Methods

### `ChannelCandidateProvider` (`backend/app/services/routing/channel_candidate_provider.py`)

Shared verbatim with [Server Channels](../server_channels/server_channels_tech.md#servicescandidateprovider). `build(db, user_id, *, policy)` returns the caller's own eligible agents, narrowed to `policy.resolved_agent_scope`. Eligibility: non-blank `router_trigger_prompt` or non-empty `example_prompts`. Reads no `AppAgentRoute`, no assignment, no `channel_app_mcp` — there is nothing of the kind left to read.

### `IdentityCandidateProvider` (`backend/app/services/routing/identity_candidate_provider.py`)

- `build(db, caller_user_id) -> list[Candidate]` -- one `Candidate` per identity **owner** (not per binding). Eligible when the caller has at least one `IdentityBindingAssignment` with `is_active AND is_enabled` whose `IdentityAgentBinding.is_active`
- `name` = owner's `full_name or email`; `trigger_prompt` = `"Contact {name} ({email}). Routes to their available agents."`; `prompt_examples` = every reachable binding's examples re-voiced as `"ask {name} ({email}) to {line}"`
- `ref_id` = `identity:{owner_id}` via `identity_ref_id()`, read back by `parse_identity_ref()` — namespaced so a person can never be looked up as an agent
- Skips are recorded, never dropped: an owner who named this caller but currently has nothing reachable is a `SKIP_IDENTITY_UNAVAILABLE` candidate

### `AppMCPRoutingService`

- `route_message(db_session, user_id, message) -> RoutingResult | None` -- resolves the channel (`ServerChannelService.get_or_create_singleton`) and policy (`ChannelPolicyService.resolve`), composes `ChannelCandidateProvider.build` with `IdentityCandidateProvider.build` (both handed the resolved `policy`; the identity provider self-gates on `policy.allow_identity_routing` and returns `[]` when it is off — there is no call-site `if`), applies the single-candidate shortcut or `_ai_classify` over the whole ballot, hands off to `_route_identity` (Stage 2) when the winner is a person. `policy.is_available` is **not** re-checked here — that is the token verifier's job, and re-checking it here would be a second copy of a rule `ChannelPolicyService` exists to own
- `_identity_pick(candidate)` -- turns an identity `Candidate` back into Stage 1's `IdentityPick` answer shape
- `_route_identity(selected_identity, caller_user_id, message, stage1_method, transformed_message)` -- Stage 2 delegation to `IdentityRoutingService.route_within_identity`; passes Stage 1's transformed message through; applies cascade logic (Stage 2 wins > Stage 1 fallback > None). No `db_session` is forwarded — Stage 2 opens its own short-lived read session
- `_ai_classify(candidates, message)` -- calls `AgentClassifier.classify(candidates, message)` directly (`backend/app/services/routing/agent_classifier.py`, not `AIFunctionsService.route_to_agent()`); resolves an identity ref **before** the UUID parse, which would otherwise reject it; returns `(Candidate | IdentityPick, transformed_message)` or `None`

`DEFAULT_SESSION_MODE = "conversation"` -- the owned-agent path's session mode; there is no longer a per-candidate session mode column to read (it used to live on the deleted route row). Only Stage 2 supplies a real one, off `IdentityAgentBinding.session_mode`.

### `AppMCPRequestHandler`

Session resolution flows through `ChannelIngestionService.assert_access` + `resolve_or_create_session` — see [channel ingestion](../agent_sessions/channel_ingestion.md) / [tech](../agent_sessions/channel_ingestion_tech.md). Message injection stays on the legacy `MessageService.create_message` + `stream_and_collect_response` pipeline due to a session-lock conflict with `initiate_stream`.

- `handle_send_message(user_id, message, context_id, mcp_ctx)` -- main tool handler: resolves session, creates message with effective (transformed) content, streams response, returns JSON
- `_try_resume_session(...)` -- resumes an existing session by `context_id` with strict `(integration_type, caller-column)` match
- New-session creation supplies `caller_id` via `extra_session_kwargs`. Identity sessions go through `ChannelIngestionService.create_identity_session` instead, which stamps `identity_caller_id` + the two identity-binding columns from an `IdentityGrant`
- Effective message: `routing_result.transformed_message or original_message`
- Session lock management: per-session `asyncio.Lock` with 500-entry cap and best-effort eviction

### `ChannelIngestionService.assert_access` — `mcp_caller` arm

The arm this phase actually rewrote. Previously: only checked `sender.platform_user_id is not None`, trusting that the routing layer had already verified a route existed. Now (`backend/app/services/sessions/channel_ingestion_service.py`):

```python
if sender.platform_user_id is None:
    raise ChannelDecline(...)
if agent.owner_id == sender.platform_user_id:
    return
grant = policy.identity_grant
if grant is None:
    raise ChannelDecline("mcp_caller on a foreign agent with no identity grant: ...")
denial = IdentityService.verify_identity_access(
    db, owner_id=grant.owner_id, binding_id=grant.binding_id,
    assignment_id=grant.assignment_id, caller_user_id=sender.platform_user_id,
    agent_id=agent.id,
)
if denial:
    raise ChannelDecline(f"mcp_caller identity grant rejected: {denial}")
```

Deliberately does **not** adopt the `channel_caller` arm's three-way `expected_owner_id` invariant — App MCP callers hold sessions on agents whose owner is the agent's owner, with `expected_owner_id` set from the agent rather than from the sender, and that has never changed.

## Frontend Components

### McpConnectorsCard (`McpConnectorsCard.tsx`)

Handles both direct MCP connector management and agent-to-agent MCP connector management for a specific agent, rendered in the agent's Integrations tab.

**Type-select step** — two options only:
- **Direct MCP Connector** — dedicated endpoint for this agent, external clients connect directly
- **Agent to Agent MCP Connector** (developer-tier only, `isDeveloper` gate) — exposes this agent over MCP so other platform agents can connect via "Connect MCP Provider"

"App MCP Server Integration" and "Identity MCP Server Integration" are gone from this dialog. App MCP exposure is automatic (trigger prompt / example prompts on the Configuration tab); identity binding creation moved to Settings → Channels → Identity Server card (see [Identity MCP Server — tech](../identity_mcp_server/identity_mcp_server_tech.md)).

### McpConnectorsCardSimple (`McpConnectorsCardSimple.tsx`)

Degraded view rendered for the `agent-user` role via `AgentIntegrationsTab`. Now takes `routerTriggerPrompt?: string | null` as a prop instead of fetching route data:

- No query, no mutation, no toggle — there is no per-user App MCP switch left to flip.
- When the prompt is blank: a dashed-border hint pointing at the Configuration tab.
- When set: two labelled read-only sections — "Available in external MCP clients" (states the agent is reachable, unconditionally, since reachability is no longer per-user) and "When this agent gets picked" (the trigger prompt, quoted as a `blockquote`, with a link to the Configuration tab to edit it).
- A footer states the negative space (this only affects MCP client reachability) and links to `/settings#channels` for the MCP Server URL.

### AppMcpServerCard (`AppMcpServerCard.tsx`)

Stripped to the one thing nothing else renders: the **MCP Server URL** and the connect walkthrough.

- Card header: copyable App MCP URL + help button (Getting Started modal, `app-mcp-setup` article).
- Body: two short explanatory paragraphs (what the URL connects to; where the on/off + agent-scope controls actually live — the `UserChannelsCard` App MCP row above it) + a "Connect instructions" button.
- **Removed**: "MCP Shared Agents" section, "Personal Routes" section, and any per-user identity toggle (that list now lives solely on `UserChannelsCard`).

**State:**
- `["mcp-info"]` -- App MCP Server URL (`staleTime: Infinity`)

### UserChannelsCard (`UserChannelsCard.tsx`)

Renders the App MCP channel row like any other channel — on/off, agent scope, `allow_identity_routing` — and is now the **sole** surface for the identity-contacts toggle list (previously duplicated, without the consent copy, on `AppAgentRoutesCard`, renamed `AppMcpServerCard.tsx` in Phase 7).

## Security

- **Admin endpoints for App MCP**: the shared `ServerChannel` admin API, `get_current_active_superuser` guard — see [Server Channels — tech](../server_channels/server_channels_tech.md#security)
- **OAuth tokens**: SHA256-hashed, stored in `app_mcp_token` table; separate from per-connector tokens; not revoked by a channel-availability change (see [Availability cache](#availability-cache-app_token_verifierpy))
- **Session isolation**: for `app_mcp` sessions, `context_id` validated against `caller_id` (not `user_id`, which is the agent owner); for `identity_mcp` sessions, validated against `identity_caller_id`
- **`mcp_caller` access on the underlying session**: `assert_access` requires ownership or a re-verified identity grant — see [Services & Key Methods](#channelingestionserviceassert_access--mcp_caller-arm) above
- **Availability fails closed**: a lookup error denies rather than allows, and is never cached — see [Availability cache](#availability-cache-app_token_verifierpy)
- **Concurrent message protection**: per-session asyncio.Lock prevents parallel processing

## Configuration

- `MCP_SERVER_BASE_URL` -- backend setting for the MCP server base URL; exposed to frontend via `/api/v1/utils/mcp-info/`
- App MCP Server URL: `{MCP_SERVER_BASE_URL}/app/mcp`
- `APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS` (default **45**) -- how long a resolved availability answer is cached per user id; the documented revocation delay
- `APP_MCP_AVAILABILITY_CACHE_MAX_ENTRIES` -- eviction bound on the in-process availability cache
