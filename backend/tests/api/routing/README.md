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
| `routing_reachability_verdict_test.py` | **The Phase 4 headline.** The `diagnosis` on `GET /traces/{id}` — one server-authored sentence per branch, plus `?expected_agent_id=` for "why was THIS agent not a candidate". One test per verdict branch, each pinning the exact sentence (pinning only `code` would let the wording drift into saying something false). **Split by origin:** the channel half is driven through real simulate decisions; the App MCP half is driven from a `seed_routing_trace(origin="app_mcp")` row, because nothing opens an App MCP capture (`ORIGIN_APP_MCP` is reserved) and that is the only producer there is. Also the Jaccard near-miss ranking, including its going quiet with a notice rather than empty when the message-text gate is closed. Branches that need a candidate list on a non-live origin, and skip reasons no surface produces any more, live in `tests/unit/test_routing_reachability.py` alongside that service's unit-level properties. |
| `routing_only_one_short_circuit_test.py` | Pass 1's **conditional** `only_one` short-circuit: all four branches of the behaviour table (one eligible candidate + empty catalog ⇒ no LLM; one + a bundle still on offer ⇒ classify, so auto-install stays reachable after onboarding; two or more ⇒ classify; zero ⇒ Pass 2), the boundary that counts *eligible* candidates rather than owned agents, `include_catalog=False` ⇒ short-circuit, and the once-only recording of the catalog scan on both paths (Pass 1 short-circuits and Pass 2 never runs; Pass 1 misses and Pass 2 reuses the scan). Plus the totality rule an availability probe has to obey: a scan that *failed* leaves the choice space unknown, and unknown must classify rather than be read as empty. |
| `routing_persist_session_ownership_test.py` | `RoutingTraceService.persist()` owns its own session: it neither commits nor rolls back the caller's transaction, does not expire the caller's ORM instances, and survives its own failure without damaging the caller. **Deliberately escapes the domain's autouse `create_session` patch** (see the module docstring) — the standard fixture rewrites `app.services.routing.routing_trace_service.create_session` to a `NonClosingSessionProxy` wrapping the test's own session, so under the normal harness `persist()` would receive the caller's session, exactly the defect the fix eliminated. Without the escape these tests would pass against the pre-fix implementation; folding this file back into the standard fixtures would silently destroy the property it exists to prove. |
| `routing_identity_stage2_capture_test.py` | **Entirely `xfail` (`strict=False`) — contributes no passing coverage today.** Both tests drive `POST /admin/routing/simulate` to pin the `identity_stage2` capture (ballot recording, and `match_method` telling the truth on a pattern hit), but that stage is unreachable: since the channel routing scope split (`e11d4020`) `simulate` is `ChannelRoutingService.decide` only, which never delegates into App MCP, so `identity_stage2` is never entered. The instrumentation in `app/services/identity/identity_routing_service.py` is intact and correct — dead instrumentation, not deleted instrumentation. Expected to come back once `docs/plans/channels_identity_unification/phase_1_identity_routing_layer.md`'s `IdentityCandidateProvider` lands; `strict=False` is deliberate so the eventual XPASS reports rather than fails. |

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
  not classify.** An empty candidate list short-circuits everywhere (and App
  MCP Stage 1 still takes `only_one` on a single effective route), so a
  scenario whose sender owns nothing eligible never reaches the classifier —
  and if one starts to, it fails instead of quietly calling a model. Do not add
  a stub "just in case": a `classify_no_match=True` on a scenario that never
  classifies disarms that signal. **Channel Pass 1's `only_one` short-circuit
  is conditional**, so "does this scenario classify?" has two inputs, not one:
  a sender owning exactly one eligible agent routes without an LLM *when the
  auto-install list holds nothing for them*, and classifies when it does. Two
  or more eligible agents always classify and must name an answer
  (`tests.utils.routing.classification(agent_id)` builds one).
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
  `tests/api/server_channels/`: give the sender an agent with its own
  `router_trigger_prompt` (`tests.utils.agent.set_router_trigger_prompt`) and
  name the classifier's answer. Channel Pass 1 reads **no `AppAgentRoute` at
  all** — a route grants nothing here — and Pass 2 needs
  `AgentClassifier.classify` patched per-test to a deterministic result (there
  is no LLM in the test environment).
- **`candidates[].prompt_examples` on a channel trace comes from
  `Agent.example_prompts`,** joined with newlines by
  `ChannelCandidateProvider`. Set it with `update_agent(..., example_prompts=[
  ...])`, as `routing_message_text_gating_test.py` does. (On the App MCP path
  it still comes from an *admin-shaped* route only: `get_effective_routes_for_
  user` sets `prompt_examples=` on the assigned-`AppAgentRoute` branch and not
  on the personal `UserAppAgentRoute` one, so a test built on
  `create_user_route` sees `prompt_examples: None` and proves nothing.)
- **`stages[].prompt` / `raw_response` / `llm_attempts` are populated only when
  Pass 1 actually classified.** A short-circuited decision has none of them by
  construction — it never rendered a prompt — so a test asserting on them needs
  a ballot of two or more eligible agents, or an auto-install list with
  something on it. (App MCP Stage 1 needs two effective routes for the same
  reason.)
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
