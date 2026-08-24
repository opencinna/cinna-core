# Auto Routing Tuning tests

API-level tests for the Auto Routing Tuning feature
(`docs/plans/auto_routing_tuning_plan.md`): a durable, superuser-only record
of every routing decision (which agents/bundles were candidates, which stage
matched, why the rest were excluded), read back through
`GET/DELETE /api/v1/admin/routing/traces` — plus the interactive half:
`POST /admin/routing/simulate`, `.../traces/{id}/replay` and
`.../traces/{id}/recommendation`.

Not split into topic groups — small enough that the domain root is the right
home; see `tests/README.md` for the split-domain threshold (~20 files,
`tests/api/agents/` is the one example today).

## Files

| File | Covers |
|---|---|
| `routing_access_control_test.py` | Superuser-only enforcement on all three routes (list/get/delete) — no partial access, not even for the sender whose own message produced the trace. |
| `routing_traces_list_and_detail_test.py` | List + get-by-id, and the single-row-per-message property: however many passes ran, one inbound message writes exactly one `routing_decision` row, with Pass 1's stages folded into Pass 2's when both ran. Also the `origin`/`outcome`/`channel_id`/`user_id` list filters. |
| `routing_error_outcome_test.py` | `outcome="error"` reachable end-to-end from an exception in either pass's thread target, the webhook still answering 200, and an earlier pass's error surviving even when a later pass recovers with a positive verdict. Also the Phase-3 regression: a Pass-1 selection that no longer resolves records an error instead of losing the trace to an FK violation. |
| `routing_message_text_gating_test.py` | `ROUTING_TRACE_STORE_MESSAGE_TEXT` both ways through the real channel path: `message_text` absent when off, `message_sha256` present and identical either way. Also the `stages` **allowlist** (`routing_trace.SAFE_STAGE_FIELDS`) in both directions — that the sender-derived fields (`prompt`, `raw_response`, `llm_attempts[].error`) are neither stored nor served with the gate off, while the agent owner's own configuration (`candidates[].trigger_prompt`, `candidates[].prompt_examples`) is. Proved by driving a real classify and reading back through the admin API, never by asserting on the constant. |
| `routing_trace_debug_link_test.py` | The live channel debug feed's `detail.trace_id` is a real key into `routing_decision`, published only when a row was actually written. |
| `routing_trace_retention_test.py` | The `ROUTING_TRACE_RETENTION_DAYS` matrix: settings validation (`>= 1`, the `-1` "keep forever" sentinel, `0` and other negatives rejected) and `RoutingTraceService.purge()` (spares everything on `-1`, deletes only rows older than the cutoff on a positive window, raises rather than returning 0 on `0` or another negative). |
| `routing_trace_clear_lifecycle_test.py` | `DELETE /admin/routing/traces` (S4): a bare unscoped delete is refused (400), `channel_id` scopes it, `?all=true` is the only way to clear everything, and every successful clear is audited as `ROUTING_TRACES_CLEARED`. |
| `routing_trace_disabled_read_gate_test.py` | `ROUTING_TRACE_ENABLED` as a READ gate (S6): `list`/`get` treat it as off — empty page + notice, 404 + a distinguishable notice — while `clear()` and the purge path deliberately ignore it (the erasure paths must keep working when tracing is off). |
| `routing_simulate_no_side_effects_test.py` | **The Phase 3 safety property.** `POST /admin/routing/simulate` binds no thread, opens no session, installs nothing and sends nothing — each asserted against durable state, never a log, and each **mutation-checked** (see the module docstring; the channel-scoped form of the outbound assertion was found unfalsifiable and replaced). Plus the §12 conditions that make the exposure acceptable: superuser-only, audited naming both admin and target without the body, rate-limited, and a response byte-identical to `GET /traces/{id}`. |
| `routing_replay_and_recommendation_test.py` | `POST /traces/{id}/replay` (a new row, the original untouched, "nothing changed" said out loud, the outcome flip after a route is added, the 409 when the text gate is off) and `POST /traces/{id}/recommendation` (drafts without writing, scoped to this trace's candidates, no audit row). Also the shared per-admin rate-limit bucket across all three LLM-spending routes. |
| `routing_reachability_verdict_test.py` | **The Phase 4 headline.** The `diagnosis` on `GET /traces/{id}` — one server-authored sentence per branch, plus `?expected_agent_id=` for "why was THIS agent not a candidate". One test per verdict branch, each pinning the exact sentence (pinning only `code` would let the wording drift into saying something false). Includes the motivating case (plan §2, Bug 2: a standalone agent has no `AppAgentRoute`, so nothing in the trace mentions it and the verdict is answered from configuration), and the Jaccard near-miss ranking, including its going quiet with a notice rather than empty when the message-text gate is closed. Unit-level properties of the same service — totality, the shared-helper reuse, candidate de-duplication — live in `tests/unit/test_routing_reachability.py`. |
| `routing_persist_session_ownership_test.py` | `RoutingTraceService.persist()` owns its own session: it neither commits nor rolls back the caller's transaction, does not expire the caller's ORM instances, and survives its own failure without damaging the caller. **Deliberately escapes the domain's autouse `create_session` patch** (see the module docstring) — the standard fixture rewrites `app.services.routing.routing_trace_service.create_session` to a `NonClosingSessionProxy` wrapping the test's own session, so under the normal harness `persist()` would receive the caller's session, exactly the defect the fix eliminated. Without the escape these tests would pass against the pre-fix implementation; folding this file back into the standard fixtures would silently destroy the property it exists to prove. |

## Key fixtures (`conftest.py`)

Same stack as `tests/api/server_channels/conftest.py` — session-proxy,
environment-adapter, background-task, and external-service stubs, plus
`patch_anyio_to_thread` — because most scenarios here drive a real webhook
delivery through `ChannelInboundService` (the only wired producer of routing
traces today; see `docs/plans/auto_routing_tuning_plan.md` §10) and channel
routing offloads its two passes onto `anyio.to_thread.run_sync`. See that
domain's own `README.md` for what each stub is for.

## Patterns specific to this domain

- **`tests/utils/routing.py`** wraps the admin read/clear API as plain HTTP
  helpers, plus a shared `post_channel_message` (mirrors
  `server_channels_routing_test.py`'s webhook-delivery helper) for driving a
  trace into existence. Two functions there are documented Rule-1 exemptions
  — `purge_routing_traces` and `seed_routing_trace` — because the
  functionality they cover genuinely has no HTTP surface (the purge
  scheduler is `TESTING`-gated like every other scheduler, and no route lets
  a caller backdate a decision's `created_at`). Read the module docstring
  before adding a third.
- **Two origins are reachable: `server_channel` and `simulate`.** The webhook
  path writes the first; `POST /admin/routing/simulate` and
  `.../traces/{id}/replay` write the second, and those rows are the only ones
  carrying `actor_user_id` (the admin who ran it) and, for a hand-typed
  simulate, a NULL `channel_id`. `app_mcp` / `identity` are still unreachable
  (plan Phase 5), so `seed_routing_trace` remains the only way to exercise the
  `origin` filter against *those*. Nothing may assume a single origin: a
  channel-scoped trace clear does not remove a channel-less simulate row, and a
  test counting "traces this delivery produced" must filter by origin or it
  will pick up an earlier simulate.
- **`AIFunctionsService.generate_router_trigger_prompt` is NOT stubbed by the
  domain fixtures** and will reach a real provider if a test lets it. The
  `patched_external_services` stack mocks `AIFunctionsService.is_available`,
  which that function never consults — an unstubbed recommendation test was
  observed returning genuine model prose. Use
  `tests.utils.routing.patched_trigger_prompt_draft`.
- **No test can reach a real model *through the classifier*, and one that
  tries fails loudly.** Note the scope: this is the classifier's provider seam,
  not a blanket ban — which is exactly why the `generate_router_trigger_prompt`
  bullet above is still live and still your responsibility. Two mechanisms,
  deliberately at different depths:
  - `block_llm_provider` (autouse, `tests/conftest.py`) patches
    `agent_classifier.get_provider_manager` for every test in the suite, in
    every domain. Function-scoped, so a session-scoped fixture would fall
    outside it; none does today.
  - `enter_classifier_patch` — the one seam behind `post_channel_message`,
    `patched_routing_externals` and `server_channels_routing_test.py`'s `_post`
    — patches `AgentClassifier.classify` to raise unless the caller names an
    answer, so the error message can say which keyword argument to pass. Pass
    the answer *through* the helper: an outer `patch(_CLASSIFY_TARGET, ...)`
    around a call is shadowed by the refusal (loudly, but shadowed).

  Both raise `UnstubbedLLMProvider`, which is a **`BaseException`**. That is
  load-bearing, not style: `ChannelRoutingService._route_installed` and
  `AgentClassifier.classify` both swallow `Exception` by design (a router
  outage must not 500 a webhook), so an ordinary exception would be caught by
  the code under test and reported as a plain no-match — a guard that passes
  its own test and is invisible exactly when it matters.

  This replaced the opposite default. Both helpers used to leave the classifier
  live when no answer was named, and sixteen call sites took that default. Two
  things the first guarded run measured, both worth knowing: none of those
  sixteen actually reaches the classifier — they short-circuit before it, so
  they were one setup change away from a live call rather than making one — and
  the four tests that *do* reach it (`routing_message_text_gating_test.py`)
  were already stubbing the provider a layer deeper. The live calls this domain
  has genuinely made came from `generate_router_trigger_prompt` and from a
  Phase-5 measurement run that exhausted a provider quota, turning a suite red
  for a reason that had nothing to do with the code.
  `tests/unit/test_routing_classifier_guard.py` pins the mechanism.
- **Passing no classifier argument now *means something*: this scenario must
  not classify.** A single effective route takes the `only_one` short-circuit
  and an empty candidate list short-circuits too, so most scenarios here never
  reach the classifier — and if one starts to, it fails instead of quietly
  calling a model. Do not add a stub "just in case": a `classify_no_match=True`
  on a scenario that never classifies disarms that signal.
- **`patched_routing_externals(classify_no_match=True)` is how a scenario gets
  a classifier that *runs and finds nothing*.** `classify_result=None` is the
  "you have not said" case, so the no-match family needs its own flag; naming
  it also reads better at the call site than `return_value=None`.
- **A test that needs the REAL render/parse path passes
  `classify_via_provider=True`** and patches
  `app.services.routing.agent_classifier.get_provider_manager` itself (see the
  bullet below on stage instrumentation). The flag removes the helper's stub
  only — the global guard stays underneath, so forgetting the provider patch
  still fails loudly rather than dialling out.
- **An assignment made for anyone other than the route's creator lands
  `is_enabled=False`.** `create_admin_route(assigned_user_ids=[...])` alone
  therefore produces a route nobody can reach; pass `auto_enable_for_users=True`
  (superuser-only) or the scenario silently becomes a different one. The
  identity equivalent has no superuser-free switch at all — the *recipient*
  turns the contact on via `toggle_identity_contact`.
- **Deterministic Pass 1 / Pass 2 setup** follows the same recipe as
  `tests/api/server_channels/`: a personal `app-mcp` route gets the
  `only_one` path with no classifier mock needed; Pass 2 needs
  `AgentClassifier.classify` patched per-test to a deterministic result (there
  is no LLM in the test environment).
- **`candidates[].prompt_examples` only ever populates from an *admin-shaped*
  route.** `AppAgentRouteService.get_effective_routes_for_user` sets
  `prompt_examples=` when it builds an `EffectiveRoute` from an assigned
  `AppAgentRoute`, and does not on the personal `UserAppAgentRoute` branch — so
  a test built on `create_user_route` sees `prompt_examples: None` on every
  candidate and would "pass" while proving nothing. Use `POST
  /agents/{id}/app-mcp-routes/` with `activate_for_myself`, as
  `routing_message_text_gating_test.py` does.
- **Two routes, not one, when a test needs Pass 1 to actually classify.** A
  single effective route takes the `only_one` short-circuit and never reaches
  the classifier, so `stages[].prompt` / `raw_response` / `llm_attempts` stay
  empty.
- **Mocking `AgentClassifier.classify` directly skips the
  `stages[].prompt` / `raw_response` instrumentation.** That boundary sits
  one layer above the code that calls `routing_trace.record_prompt` /
  `record_raw_response` (inside `AgentClassifier.classify` itself), so
  every test that patches it — which is most of this domain, by design, to
  stay deterministic — produces a trace where those two stage fields are
  always `None`, independent of `ROUTING_TRACE_STORE_MESSAGE_TEXT`. A test
  that needs those fields genuinely populated (see
  `routing_message_text_gating_test.py`'s S5 test) has to mock one layer
  deeper instead — `app.services.routing.agent_classifier.get_provider_manager`
  — so the real render/parse runs and the real instrumentation fires.

  Phase 5 moved both of these. The classifier used to be
  `AIFunctionsService.route_to_agent` and the provider seam used to be
  `app.agents.app_agent_router.get_provider_manager`; the three near-copies of
  the candidate-building code collapsed into `AgentClassifier`, and
  `app_agent_router` is now a thin `list[dict]` adapter over it.
- **`GET /admin/routing/traces` ordering has a random tiebreak — don't index
  into the list.** The route orders by `created_at DESC, id DESC`, and two
  deliveries produced within a single test can land close enough in time that
  the tiebreak — a random UUID — is what actually decides the order. This has
  already bitten once: a test indexed into the response (`data[0]`,
  `data[1]`) for a same-test multi-row scenario, and it silently asserted
  against the wrong trace while still appearing to pass. The working pattern
  is to snapshot the existing trace ids *before* the delivery, then diff
  against the post-delivery list and assert on exactly the one new id —
  never on position.
