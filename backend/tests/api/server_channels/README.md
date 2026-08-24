# Server Channels tests

API-level tests for the Server Channels feature (`docs/application/server_channels/server_channels_tech.md` — business logic in the sibling `server_channels.md`; `docs/plans/server_channels_plan.md` is the original implementation plan, now a historical artifact rather than a description of what was built):
admin-configured channels (Google Chat first) that let external people reach
platform agents from outside the platform, routed and bound per-thread to a
session.

Not split into topic groups — small enough (11 files) that the domain root is
the right home; see `tests/README.md` for the split-domain threshold (~20
files, `tests/api/agents/` is the one example today).

## Files

| File | Covers |
|---|---|
| `server_channels_security_invariants_test.py` | The three security properties called out by the feature review: cross-user thread gate, lost-race ownership refusal (both the ingest and the park branch), and the malformed-JWT probe family (403, never 500). Also the deliberate `critical_state` plan deviation. Read this file first — everything else is secondary to these invariants staying green. |
| `server_channels_admin_test.py` | Admin CRUD lifecycle, superuser-only enforcement, secrets never echoed, webhook-token regeneration, auto-install list CRUD + visibility/trigger-prompt badges. |
| `server_channels_webhook_test.py` | 404 on unknown/disabled token, ignored/added_to_space event handling, the whitelist matrix (patterns, fail-closed, case-insensitivity), auto-register on/off + idempotency, redelivery dedup. |
| `server_channels_routing_test.py` | Pass 1 (the sender's own agents) match, candidate scoping (`ChannelCandidateProvider`: foreign agents absent from the ballot, identity contacts absent from the ballot entirely, ineligible owned agents recorded as skips), Pass 2 (auto-install catalog) candidate filtering, no-match reply, binding self-heal (`failed` → re-route), session-deleted recovery. |
| `server_channels_debug_test.py` | The admin debug feed (pipeline decision visible per event, verification failures captured with no payload detail, superuser-only) and the test-send targeting rework (exactly-one-target validation, the unseen-email explanation, email → observed thread end to end). |
| `server_channels_user_settings_test.py` | The per-user settings routes (`GET/PUT/DELETE /users/me/channels`). `PUT` is the only thing in the codebase that creates a `channel_user_setting` row, so lazy creation on first edit is the headline fact here. Also: the inherit/override provenance matrix (an admin default flip is followed by an inheriting user, not by one with an explicit value, in both directions), `DELETE` reverting to pure inheritance and a later admin default change then being followed, cross-user ownership isolation on all three verbs, and the user projection's secret-adjacent-field defence (checked as an allowlist of actual response keys, not a blocklist of known secret names). |
| `server_channels_identity_routing_test.py` | **The phase-3 headline: the HR story, end to end.** A sender who owns nothing reaches an identity owner's agent from Google Chat — session in the OWNER's workspace, `identity_caller_id` the sender, `integration_type` still `channel_*`, **and the reply delivered back to the sender's own thread** (the trap: `ChannelOutboundService._resolve_channel_session` gates on the `channel_` prefix, so an `identity_mcp` stamp would route, answer, and silently never deliver). Also the one-candidate short-circuit that makes the story classifier-free, the second and third messages resuming the same session (what `_verify_resume_sender`'s exception was relaxed for), that exception's own condition forged in both directions, session visibility pinned on all three surfaces (`GET /sessions/` as sender: absent; as owner: present; `GET /external/sessions` as sender: present), thread ownership staying the sender's on both the synchronous and lost-race paths, and the absent-grant refusal on the session-deleted recovery branch. |
| `server_channels_identity_revocation_test.py` | Withdrawing permission, from either side: the owner revoking **between the decision and the ingest** (the reason `assert_access` re-reads rather than trusts — driven by revoking from inside the Stage-2 classifier call), the owner revoking mid-thread, the sender withdrawing their own `allow_identity_routing` consent mid-thread (and `DELETE /users/me/channels/{id}` counting as a withdrawal), the per-person contact toggle unchanged, an ordinary non-identity thread unaffected by the switch, and the `SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED` audit row (transitions only, `medium`, explicit `null` is a 422). Every refusal asserts the generic reply **verbatim** — they must be indistinguishable, or the reply is an oracle. Its module docstring also records why the scheduler-drain variant of consent withdrawal is unreachable by construction. |
| `server_channels_identity_trace_test.py` | What an identity decision writes into the routing trace and what it must not: the deliberate silence when the switch is off (no `identity_unavailable` skip, no `identity:` `ref_id`, no `source="identity"` — the one inversion of master plan §3.5, paired with the switched-on delivery so the absence is not vacuous), the `identity_stage2` ballot with each binding's trigger prompt and `prompt_examples`, `match_method` in the current vocabulary (`only_one` on the shortcut, never `pattern`), a binding glob no longer winning, and the `SKIP_IDENTITY_UNAVAILABLE` reachability verdict. **Replaces `tests/api/routing/routing_identity_stage2_capture_test.py`**, which was deleted: simulate has no `channel_id`, so `ResolvedChannelPolicy.for_no_channel()` keeps `allow_identity_routing` false and identity can never enter a simulated ballot. |
| `server_channels_pending_outbound_test.py` | `flush_pending_bindings` parking/flush/env-failure paths, `STREAM_COMPLETED` outbound gating + binding lookup. |
| `server_channels_policy_test.py` | `ChannelPolicyService` observed through a real routing decision (Phase 2 of the channels/identity refactor): no-settings-row inheritance for an auto-registered sender who can never have one, `visibility="restricted"` declining with the same reply shape as a whitelist miss and routing once granted, `channel.enabled=False` overriding an explicit user `is_enabled=True` (proved as total invisibility — the webhook 404s and the user-facing routes 404/omit, since a disabled channel is filtered out before `ChannelPolicyService` is ever consulted), `agent_scope="list"`/`"none"` recording out-of-scope owned agents as skips rather than absences, `pinned_agent_id` skipping classification while still leaving a `match_method="pinned"` trace row (and self-healing when the pinned agent is deleted or changes hands), `allow_auto_install=False` and a raced pin both barring Pass 2 with their own trace note (`PASS_2_NOT_ALLOWED_NOTE` / `PASS_2_PINNED_NOTE`), that note being suppressed when Pass 1 already recorded an error, and the decline gate applying to an already-bound thread on all three revocation shapes (admin disables the channel, admin withdraws a grant, user switches it off). |

Chunking (`GoogleChatAdapter._chunk`) is pure text-splitting logic with no I/O
and is unit-tested instead: `tests/unit/test_google_chat_adapter_chunk.py`.
The debug buffer's bounds (ring-buffer eviction, text clamp, per-channel
isolation) are likewise pure in-memory logic:
`tests/unit/test_channel_debug_buffer.py`.

## Key fixtures (`conftest.py`)

Same stack as `tests/api/agents/conftest.py` (session-proxy, environment
adapter stub, background-task collector, external-service mocks, storage
dirs), plus `reset_channel_debug_buffer` (the debug buffer is process-global class
state that every webhook test now fills — reset on both sides so tests never
see each other's events) and `patch_anyio_to_thread`: the inbound pipeline offloads routing via
`anyio.to_thread.run_sync` (not `asyncio.to_thread`), so it needs its own
patch to stay on the test thread/transaction.

## Patterns specific to this domain

- **No binding read API.** `ChannelThreadBinding` has no admin/user-facing GET
  endpoint by design (it is internal pipeline state). Binding lifecycle is
  verified through *observable* effects instead: the webhook's synchronous
  reply text (`REPLY_WORKING`, `REPLY_STILL_SETTING_UP`, `REPLY_THREAD_OWNED`,
  …), session creation/count via `GET /sessions/`, and outbound adapter calls.
- **Deterministic Pass 1 setup.** Give the sender an agent with its own
  `router_trigger_prompt` (`tests/utils/agent.py::set_router_trigger_prompt`).
  Channel Pass 1 builds its candidates from the **agents the sender owns**
  (`ChannelCandidateProvider`) and reads no `AppAgentRoute` — a personal or
  admin route grants nothing here.
- **Whether you must name a classifier answer depends on the *catalog*, not
  only on the ballot.** Pass 1's `only_one` short-circuit is conditional: one
  eligible candidate **and** nothing Pass 2 could offer this sender routes
  without an LLM (`match_method="only_one"`), because there is no alternative
  to choose between. So the common single-agent setup with an empty
  auto-install list needs **no** classifier answer — and naming none is the
  stronger form, since the stub raises if the classifier is reached after all.
  Name one (`classify_result=tests.utils.routing.classification(agent_id)`)
  when the sender owns two or more eligible agents, or when the auto-install
  list holds something they could still be offered.
- **Pass 2 needs a mocked classifier too.** `AgentClassifier.classify` is
  patched per-test (`app.services.ai_functions.ai_functions_service
  .agent_classifier.AgentClassifier.classify`) to hand back a chosen bundle
  deterministically — there is no LLM in the test environment.
- **Lost-race "park branch" without real concurrency.** `drain_tasks()` runs
  collected background tasks strictly sequentially (no interleaving), so a
  genuine two-in-flight race can't be reproduced directly. The park branch
  (`ChannelInboundService._handle_lost_race`, same-user + `pending_install`)
  is instead driven deterministically: queue two webhook deliveries for the
  *same* user/thread before draining, with a `side_effect` classifier that
  picks whichever single candidate is on the ballot — Pass 1's ballot holds
  agent ids and Pass 2's holds bundle ids, and one fixed answer cannot serve
  both. See
  the test docstring in `server_channels_security_invariants_test.py` for the
  full walkthrough.
- **JWT verification uses a real signer, not a mock.** `tests/utils
  /server_channel.py::GoogleChatJWTSigner` generates a throwaway RSA keypair
  and JWKS and only patches the JWKS *fetch*
  (`app.core.security._get_google_certs`); the JWT itself is genuinely
  RS256-signed and genuinely verified by `verify_google_signed_jwt`. This is
  what lets the malformed-JWT tests prove the real fix (Authlib's bare
  `ValueError` on an unknown `kid`) rather than a mocked-away one.
- **Identity scenarios: two helpers and one shape to remember.**
  `tests/utils/identity.py::share_identity_agent` composes the two calls the
  product needs in order — the OWNER creates the binding with
  `assigned_user_ids`, then the RECIPIENT enables the contact — because an
  assignment created by anyone but a superuser lands `is_enabled=False` and
  `auto_enable=True` is superuser-only, so a binding alone reaches nobody.
  `allow_identity_routing` is switched on through the real route
  (`tests/utils/user_channel.py::update_my_channel`); it never inherits, so
  there is no admin-side way to set it and no channel default to flip.
  The shape worth internalising: a sender who owns **no** agent and can reach
  **one** identity owner produces a one-candidate ballot, so Pass 1 takes its
  `only_one` short-circuit and — if that owner has one reachable binding —
  Stage 2 takes its own. **Nothing classifies**, and naming no classifier
  answer is therefore the correct and stronger form. Give the owner two
  bindings when a test needs Stage 2 to actually classify.
- **`verify_resume_sender` is a documented Rule-1 exemption**
  (`tests/utils/server_channel.py`), added for one condition that has no HTTP
  path: `_verify_resume_sender`'s identity exception. Every production route
  into it passes through `ChannelInboundService._ingest`, which refuses a
  `user.id != binding.user_id` pair at its own entry — so a third party is
  stopped a layer earlier by a *different* guard, and the exception's own
  condition can only be reached by forging the sender. The service comment
  claims it stands alone; this executes the claim.
- **`flush_pending_bindings` is called directly.** It has no HTTP surface —
  the scheduler that calls it in production is `TESTING`-gated — so
  `tests/utils/server_channel.py::flush_pending_bindings` is a documented
  Rule-1 exemption (same pattern as `tests/utils/platform_token.py` and the
  active-streaming-manager helpers in `tests/utils/session.py`).
