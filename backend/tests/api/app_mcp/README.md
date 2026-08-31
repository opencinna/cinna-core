# App MCP tests

App MCP is the MCP server the platform exposes to a user's own MCP client
(`/mcp/app/mcp`): the caller authenticates with an `app_mcp_token`, sends a
message, and the platform routes it to one of their agents — or, when they have
opted in, to a *person* whose agents then answer on their behalf.

Since phase 5 of `docs/plans/channels_identity_unification/` App MCP is a
`ServerChannel` like Google Chat and email — a **singleton** one
(`adapters/app_mcp.py`, `is_singleton=True`), materialized lazily rather than
created by anybody — so the admin kill switch, the channel's visibility + grant
allowlist, the per-user toggle and the resolved `ResolvedChannelPolicy` all
apply to it, and its ballot is composed from the same two candidate providers
every other surface uses. Most of what used to be App-MCP-specific now lives in
`tests/api/server_channels/` and `tests/api/routing/`; what is left here is what
only this surface has.

Not split into topic groups — well under the ~20-file threshold `tests/README.md`
names.

## Files

| File | Covers |
|---|---|
| `app_mcp_session_test.py` | `AppMCPRequestHandler.handle_send_message()`: session creation and `context_id` reuse, `session.user_id` = the agent owner while `caller_id` = the caller, the cross-integration isolation guard, the identity-session path and its metadata (including the fallback to the caller's email when `full_name` is empty). Also **both halves of identity revocation on resume**: the owner's (binding deactivated) and the caller's own (`allow_identity_routing` switched off mid-conversation) — the latter re-read per message on resume for parity with the channel path, refused with the same words so the caller cannot tell which switch closed, and asserted not to fall through into a *fresh* identity session. |
| `app_mcp_channel_availability_test.py` | Availability is enforced at token **verification**, not at token issue: a token minted while the channel was open stops working the moment it closes, and is refused with the same `None` a forged one gets. Also the three properties of the per-user availability cache. |
| `app_mcp_identity_candidate_provider_test.py` | `IdentityCandidateProvider.build()` — two bindings from one owner collapse into one candidate with the pre-refactor name/trigger text pinned verbatim, and the exact boundary between a *skipped* candidate and no row at all. |
| `app_mcp_routing_trace_test.py` | `AppMCPRoutingService.route_message()` writes an `origin="app_mcp"` routing trace with a populated `channel_id`, and `ROUTING_TRACE_APP_MCP_MODE` (`off` \| `metadata` \| `full`) governs how much of it lands — including that `off` suppresses the capture rather than failing it quietly, and that `metadata` narrows `ROUTING_TRACE_STORE_MESSAGE_TEXT` rather than re-opening it. |
| `app_mcp_oauth_flow_test.py` | The DCR/authorize/consent/token-exchange path, and specifically the fallbacks for clients that registered without a `resource` and so landed in the per-connector `MCPOAuthClient` table. |
| `prompt_examples_test.py` | `IdentityAgentBinding.prompt_examples` lifecycle and its write-time validator. (The `AppAgentRoute` half went with that family in phase 5; App MCP's own examples now come from `Agent.example_prompts`, clamped at render time instead.) |

## Patterns specific to this domain

- **There is no HTTP route to drive, so tests enter at the service layer — and
  that is this directory's documented convention, not an exemption taken case
  by case.** App MCP is an MCP tool-call surface. `app_mcp_session_test.py`
  calls `handle_send_message()` directly ("not through MCP protocol", as its
  docstring says); `app_mcp_identity_candidate_provider_test.py` calls
  `IdentityCandidateProvider.build()`; `app_mcp_routing_trace_test.py` calls
  `AppMCPRoutingService.route_message()`. Each names the depth it enters at and
  why. **Every *input* to the behaviour under test is still created through
  real routes** — agents, trigger prompts, identity bindings, tokens, channel
  configuration — and every durable result is read back through one
  (`GET /admin/routing/traces`, `GET /sessions`, the admin channel routes). Pick
  the shallowest entry point that actually contains the behaviour: entering at
  `handle_send_message()` to observe a routing decision drags an environment
  stub and the streaming pipeline in behind it.
- **`conftest.py` extends the agent-domain fixtures with two App MCP session
  targets.** `CREATE_SESSION_TARGETS_APP_MCP` adds
  `app_mcp_request_handler.create_session` and
  `app.mcp.app_token_verifier.create_session` — the verifier opens its own
  sessions for the token lookup and, through `ServerChannelService`, for the
  channel's availability policy. Without both, a token minted by the OAuth flow
  reads as "not found" and the lazily-materialized channel row is written to the
  **real** database. `RoutingTraceService.persist` also opens its own session;
  that one is already covered by `CREATE_SESSION_TARGETS_AGENT`.
- **The App MCP channel row is never created by a test.** It is a singleton,
  materialized by whatever asks for it first — the token verifier, the admin
  listing, or `route_message` itself. Reach it with
  `tests.utils.server_channel.find_server_channel_by_type(client, headers,
  "app_mcp")`, which asserts there is exactly one; "the first of several" would
  quietly pass the test that is supposed to prove there can only be one. It also
  cannot be deleted (`server_channel_service.py`'s singleton guard), which
  matters more now that `RoutingDecision.channel_id` cascades on delete.
- **No LLM is reachable, and a forgotten stub fails loudly.** The autouse
  `block_llm_provider` guard (`tests/conftest.py`) patches
  `app.services.routing.agent_classifier.get_provider_manager` for every test in
  the suite; an unstubbed classify raises `UnstubbedLLMProvider`, a
  `BaseException`, so the `except Exception` inside `AgentClassifier.classify`
  cannot swallow it. **Do not add a stub "just in case":** a caller who owns
  exactly one eligible agent takes Stage 1's `only_one` short-circuit and must
  never reach a model, and naming no classifier answer is what asserts that. A
  test that genuinely needs a rendered prompt and a parsed reply on the trace
  gives its caller **two** eligible agents and patches `get_provider_manager`
  itself — one layer *below* `AgentClassifier.classify`, because that is where
  `record_prompt` / `record_raw_response` fire. See
  `tests/api/routing/README.md` for the longer form of both rules; they apply
  here unchanged.
- **An agent becomes an App MCP candidate by having a
  `router_trigger_prompt` or `example_prompts`, and by nothing else.** There is
  no route, no assignment and no per-agent toggle to create — the
  `AppAgentRoute` family was deleted in phase 5. Use
  `tests.utils.agent.set_router_trigger_prompt`.
- **Identity contacts are opt-in, per person, by the recipient.**
  `allow_identity_routing` defaults false and never inherits, so a test that
  wants a person on the ballot must both create the binding *and* have the
  caller turn the contact on (`toggle_identity_contact`), unless the binding was
  created with `auto_enable=True`.
- **Routing traces written from this directory are subject to
  `ROUTING_TRACE_APP_MCP_MODE`.** At the `metadata` default an `app_mcp` row
  carries no `message_text` and no `stages[].prompt` / `.raw_response`; at `off`
  there is no row at all. A test that reads any of those fields must pin the
  mode. Read traces back by **diffing trace ids** across the call rather than
  indexing into the list: `GET /admin/routing/traces` orders by
  `created_at DESC, id DESC` with a random-UUID tiebreak.
