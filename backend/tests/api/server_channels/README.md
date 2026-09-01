# Server Channels tests

API-level tests for the Server Channels feature (`docs/application/server_channels/server_channels_tech.md` — business logic in the sibling `server_channels.md`; `docs/plans/server_channels_plan.md` is the original implementation plan, now a historical artifact rather than a description of what was built):
admin-configured channels (Google Chat first) that let external people reach
platform agents from outside the platform, routed and bound per-thread to a
session.

Not split into topic groups — small enough (16 files) that the domain root is
the right home; see `tests/README.md` for the split-domain threshold (~20
files, `tests/api/agents/` is the one example today).

## Files

| File | Covers |
|---|---|
| `server_channels_security_invariants_test.py` | The three security properties called out by the feature review: cross-user thread gate, lost-race ownership refusal (all three entry points — the ingest and park branches reached from Pass 1, and `_install_and_park`'s own `if not created:` edge reached from Pass 2 after an auto-install), and the malformed-JWT probe family (403, never 500). Also the deliberate `critical_state` plan deviation. Read this file first — everything else is secondary to these invariants staying green. |
| `server_channels_admin_test.py` | Admin CRUD lifecycle, superuser-only enforcement, secrets never echoed, webhook-token regeneration, auto-install list CRUD + visibility/trigger-prompt badges. |
| `server_channels_webhook_test.py` | 404 on unknown/disabled token, ignored/added_to_space event handling, the whitelist matrix (patterns, fail-closed, case-insensitivity), auto-register on/off + idempotency, redelivery dedup. |
| `server_channels_routing_test.py` | Pass 1 (the sender's own agents) match, candidate scoping (`ChannelCandidateProvider`: foreign agents absent from the ballot, identity contacts absent from the ballot entirely, ineligible owned agents recorded as skips), Pass 2 (auto-install catalog) candidate filtering, no-match reply, binding self-heal (`failed` → re-route), session-deleted recovery. |
| `server_channels_debug_test.py` | The admin debug feed (pipeline decision visible per event, verification failures captured with no payload detail, superuser-only) and the test-send targeting rework (exactly-one-target validation, the unseen-email explanation, email → observed thread end to end). |
| `server_channels_user_settings_test.py` | The per-user settings routes (`GET/PUT/DELETE /users/me/channels`). `PUT` is the only thing in the codebase that creates a `channel_user_setting` row, so lazy creation on first edit is the headline fact here. Also: the inherit/override provenance matrix (an admin default flip is followed by an inheriting user, not by one with an explicit value, in both directions), `DELETE` reverting to pure inheritance and a later admin default change then being followed, cross-user ownership isolation on all three verbs, and the user projection's secret-adjacent-field defence (checked as an allowlist of actual response keys, not a blocklist of known secret names). |
| `server_channels_identity_routing_test.py` | **The phase-3 headline: the HR story, end to end.** A sender who owns nothing reaches an identity owner's agent from Google Chat — session in the OWNER's workspace, `identity_caller_id` the sender, `integration_type` still `channel_*`, **and the reply delivered back to the sender's own thread** (the trap: `ChannelOutboundService._resolve_channel_session` gates on the `channel_` prefix, so an `identity_mcp` stamp would route, answer, and silently never deliver). Also the one-candidate short-circuit that makes the story classifier-free, the second and third messages resuming the same session (what `_verify_resume_sender`'s exception was relaxed for), that exception's own condition forged in both directions, session visibility pinned on all three surfaces (`GET /sessions/` as sender: absent; as owner: present; `GET /external/sessions` as sender: present), thread ownership staying the sender's on both the synchronous and lost-race paths, and the absent-grant refusal on the session-deleted recovery branch. |
| `server_channels_identity_revocation_test.py` | Withdrawing permission, from either side: the owner revoking **between the decision and the ingest** (the reason `assert_access` re-reads rather than trusts — driven by revoking from inside the Stage-2 classifier call), the owner revoking mid-thread, the sender withdrawing their own `allow_identity_routing` consent mid-thread (and `DELETE /users/me/channels/{id}` counting as a withdrawal), the per-person contact toggle unchanged, an ordinary non-identity thread unaffected by the switch, and the `SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED` audit row (transitions only, `medium`, explicit `null` is a 422). Every refusal asserts the generic reply **verbatim** — they must be indistinguishable, or the reply is an oracle. Its module docstring also records why the scheduler-drain variant of consent withdrawal is unreachable by construction. |
| `server_channels_identity_trace_test.py` | What an identity decision writes into the routing trace and what it must not: the deliberate silence when the switch is off (no `identity_unavailable` skip, no `identity:` `ref_id`, no `source="identity"` — the one inversion of master plan §3.5, paired with the switched-on delivery so the absence is not vacuous), the `identity_stage2` ballot with each binding's trigger prompt and `prompt_examples`, `match_method` in the current vocabulary (`only_one` on the shortcut, never `pattern`), a lexical hit on a binding's trigger prompt never beating the classifier's verdict (the glob it replaces can no longer be fed at all — `message_patterns` is gone as a column), and the `SKIP_IDENTITY_UNAVAILABLE` reachability verdict. **Replaces `tests/api/routing/routing_identity_stage2_capture_test.py`**, which was deleted: simulate has no `channel_id`, so `ResolvedChannelPolicy.for_no_channel()` keeps `allow_identity_routing` false and identity can never enter a simulated ballot. |
| `server_channels_pending_outbound_test.py` | `flush_pending_bindings` parking/flush/env-failure paths, `STREAM_COMPLETED` outbound gating + binding lookup. |
| `server_channels_app_mcp_test.py` | App MCP as a channel (Phase 5 of the channels/identity refactor): the **singleton** row nothing creates — materialized lazily by `ServerChannelService.get_or_create_singleton`, so its presence in a listing *is* the assertion that the one shared accessor ran — its authenticated-transport projection (no webhook token or URL, `has_outbound_credentials=True` because nothing is missing rather than because something is stored, empty `config`, NULL-and-inert `email_whitelist`), the `/channel-types` transport shape the admin form branches on instead of on `channel_type`, and the four refusals: a second `app_mcp` channel (409), a `channel_type` patch moving an existing channel onto the type (the same 409, and the door that is easier to forget), any config at all (422), a webhook-token regeneration (422), and deletion (422 — the row would be re-materialized with **default** settings, turning "delete" into a silent reset of the kill switch). Plus the user surfacing: a brand-new user sees the row in Settings → Channels with no settings row of their own, and it follows `visibility`/grants/`enabled` like any channel. The enforcement half — that those switches actually refuse a live App MCP token — is `tests/api/app_mcp/app_mcp_channel_availability_test.py`, which needs the OAuth flow and that domain's fixtures. |
| `server_channels_status_notice_test.py` | The thread **status notice**: one message the pipeline posts, rewrites in place as the work advances (routing → installing → working), and rewrites one final time to hold the agent's own reply — replacing the three permanent notices a first contact used to leave behind. The reply takes the notice's **slot** rather than being posted under it and the notice deleted, because Chat renders a deletion as a "Message deleted by its author" tombstone and that appeared above every answer; deletion survives only for a turn with nothing to say. Covers the full post → patch → become-the-reply life of one notice, the second turn (silent before, and the place the **released id** is observable — a retained one would patch the next spinner over the previous answer), no-match **settling** the notice instead of posting under it, and the install → flush hand-off, which is the reason the id lives on the binding at all: "ready" is emitted minutes later from the scheduler task with nothing but the binding row to go on. Also the sync-response regression guard — an accepted message must ack `{}`. |
| `server_channels_policy_test.py` | `ChannelPolicyService` observed through a real routing decision (Phase 2 of the channels/identity refactor): no-settings-row inheritance for an auto-registered sender who can never have one, `visibility="restricted"` declining with the same reply shape as a whitelist miss and routing once granted, `channel.enabled=False` overriding an explicit user `is_enabled=True` (proved as total invisibility — the webhook 404s and the user-facing routes 404/omit, since a disabled channel is filtered out before `ChannelPolicyService` is ever consulted), `agent_scope="list"`/`"none"` recording out-of-scope owned agents as skips rather than absences, `pinned_agent_id` skipping classification while still leaving a `match_method="pinned"` trace row (and self-healing when the pinned agent is deleted or changes hands), `allow_auto_install=False` and a raced pin both barring Pass 2 with their own trace note (`PASS_2_NOT_ALLOWED_NOTE` / `PASS_2_PINNED_NOTE`), that note being suppressed when Pass 1 already recorded an error, and the decline gate applying to an already-bound thread on all three revocation shapes (admin disables the channel, admin withdraws a grant, user switches it off). |
| `server_channels_streaming_updates_test.py` | The status notice **streaming**: while the agent writes, the notice stops being a spinner and becomes a rolling draft — the same message patched with the answer so far, each state a strict prefix of the next. Covers the draft growing and the finished reply taking the same slot with the id released, `thinking` never reaching the thread (a distinct stream event type, asserted against every byte the four verbs were handed, and paired with a positive adjacency check so it cannot pass by the turn having produced nothing), the **seal** — draft settled at a paragraph break, a fresh message opened below it, and the reply landing in the *new* one, which is how `status_message_id` being repointed is observed without a read API — the `CHANNEL_STREAM_UPDATES_ENABLED=False` regression guard (strict list equality on all four verbs against the identical stream, which is what protects every pre-existing channel reply), and a mid-stream failure settling the apology **under** the half-answer instead of over it. |
| `server_channels_stop_command_test.py` | The `/stop` channel control command and the acknowledgement it leans on. The command half: `interrupt_stream` called for the thread's own session with the text never ingested, the "nothing running" decline produced by the real `ValueError` path, a stranger's `/stop` getting `REPLY_THREAD_OWNED` and never reaching the registry (**invariant 6** — interception sits strictly after the ownership gate, which is where its authorization comes from), and a Chat retry acknowledged once via the `:control:` dedup key. The three shapes that are deliberately *not* a command: a first-message `/stop` (no binding, nothing to stop — it routes like any text), one with a file attached, and one on a `pending_install` binding, which must be answered and dropped rather than parked and replayed at the brand-new assistant. The acknowledgement half — four shapes of stopped turn that are **not** interchangeable: a partial answer keeps its text with the marker under it; a turn stopped **before the agent spoke** still says so (the likeliest stop there is, and the one that shipped as a stranded spinner); a relay that already sealed everything gets the marker as a fresh message **below** the sealed text (the mirror bug settled a bare marker over a live draft); and a thread that was never narrating is told **nothing** — the branch that also holds email at zero behaviour change. |
| `server_channels_turn_identity_test.py` | **Turn identity**: a turn's reply is the message *that turn* wrote, never the newest agent row in the session. The headline is the stale-turn bug's reproducer — turn one answers, turn two is a `/run:*` **command turn** (which writes only a `role="system"` message and never an agent row), and the thread must not be shown turn one's answer again; it fails on pre-turn-identity code. Its email twin is the other shape that writes no agent row (a batch with no storable events) on the one transport that never attaches a relay, so every reply there resolves through the arm this feature changed. Also the `channel_turn_delivery` ledger over real turns: draft→sealed→final with dense part indexes (paired with the kill switch as its control), a duplicate `STREAM_COMPLETED` delivering once (paired with the *same* replay seam on an unsettled turn, which does deliver), a **failed** final row being retried and corrected in place rather than inserted onto the unique constraint, an interrupted turn's rows being attributed to itself so the next completion cannot adopt them, and the divergence check marking a row while changing nothing the reader sees. The interrupted acknowledgement is re-pinned here as a regression guard on the ledger close-out that now sits in that handler's `finally`. |

Chunking (`GoogleChatAdapter._chunk`) is pure text-splitting logic with no I/O
and is unit-tested instead: `tests/unit/test_google_chat_adapter_chunk.py`.
So are the other pure pieces of the outbound path — the CommonMark → Chat
markup translation (`tests/unit/test_google_chat_format.py`), the webhook's
own reply body (`tests/unit/test_google_chat_sync_response.py`, which pins the
`thread` field that keeps a sync reply out of the space and inside the
conversation), and `replace_message`'s composition
(`tests/unit/test_google_chat_replace_message.py`: which chunk takes the slot,
the fallback when the patch fails, the message-resource-name guard, and the
partial failure — a patch that landed followed by a remainder that did not
still reports `replaced=True`, because ownership of that message transferred
at the patch and losing it lets the next turn's spinner overwrite a delivered
reply). The status-notice verbs' **totality** — `set_status` must return
`None`, never raise, when its adapter lookup hits an expired instance or a
poisoned session — is pinned in
`tests/unit/test_channel_outbound_instrumentation.py`; three callers settle
notices through it from inside failure handlers that have nowhere left to put
an exception.
The debug buffer's bounds (ring-buffer eviction, text clamp, per-channel
isolation) are likewise pure in-memory logic:
`tests/unit/test_channel_debug_buffer.py`.

The turn-delivery feature's pure halves live in `tests/unit/` too, and the
split is worth knowing before adding to either side: the **emitter** contract
(every terminal stream event carries `agent_message_id`, and an explicit
`None` survives the `**extra_meta` splat as a *present* key) is
`test_stream_turn_identity_meta.py`, which reads `message_service`'s own syntax
tree because the property is about all five emission sites at once and no
runtime test of one path can assert it; the **consumer** branch matrix (a uuid
/ a row that has gone / an explicit `None` / the key absent entirely / the
read itself raising) is `test_channel_turn_identity_consumer.py`, where the
last two are the ones that shipped wrong — `meta.get(key)` cannot tell "absent"
from "present and `None`", and a failed read folded into `None` deletes the
notice a broken relay's partial answer is standing in; and the ledger's own
state machine plus its totality is `test_channel_turn_delivery_ledger.py`. The
relay's boundary bookkeeping (one row per external message, the draft row
settled *in place* by a seal, the row released at each batch hand-over) is in
`test_channel_stream_relay.py` alongside the rest of that module.

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
  reply text (`REPLY_STILL_SETTING_UP`, `REPLY_THREAD_OWNED`, `REPLY_DENIED`,
  …), session creation/count via `GET /sessions/`, and outbound adapter calls.
  **An accepted new-thread message is not one of them any more.** Its
  narration moved into the status notice, so the sync body is `{}` and
  `REPLY_WORKING` is observed as a `send_message` call — see
  `server_channels_status_notice_test.py`. Tests asserting what a sender was
  *told* should filter the notice's intermediate states out (the pattern is
  `_STATUS_NOTICE_TEXTS` in `server_channels_identity_revocation_test.py`):
  against the real adapter those are `patch` calls on one message, but a
  mocked `send_message` cannot return a usable id so each state falls back to
  a fresh post and shows up in the call list. A test that wants the real
  behaviour must mock **all four** verbs — `send_message`, `update_message`,
  `replace_message`, `delete_message` — and hand back a real-shaped
  `spaces/AAA/messages/…` id from `send_message`; anything else is refused by
  `GoogleChatAdapter._message_url` and degrades to a plain post. See `_Chat`
  in `server_channels_status_notice_test.py`.
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
- **The turn-delivery ledger is read straight off the table.** Same posture
  as the notice-id exemption below, one table further down and for a sharper
  reason: `ChannelTurnDelivery` has no API surface *at all*, and the thing
  worth asserting about it — which rows exist, what turn they are attributed
  to, which one is `final`, which one is `diverged` — is invisible from the
  thread **by construction**, because the divergence check is deliberately
  observational and delivers nothing in either outcome. So
  `tests/utils/server_channel.py::list_turn_deliveries` reads one thread's
  rows in delivery order. Everything the ledger *changes* about what a reader
  sees is still asserted through the four adapter verbs like the rest of this
  domain. A duplicate `STREAM_COMPLETED` has no HTTP route either — nothing in
  the API surface can emit the same completion twice for one batch — so
  `replay_stream_completed` hands the bus subscriber the same event again,
  the `deliver_via_binding` shape of exemption.
- **The status notice id has one, narrow, read-only exemption.** "No binding
  read API" (above) is the default and stays the default — every lifecycle
  fact but one is still verified through an observable effect. The exception
  is `tests/utils/server_channel.py::get_binding_status_message_id`: a test
  that wants to pin "the id was actually released" as its own fact, rather
  than only infer it from a later turn behaving as if it were, has no other
  seam to reach `ChannelThreadBinding.status_message_id` through. See
  `server_channels_status_notice_test.py::
  test_the_notice_id_is_cleared_on_the_binding_between_turns`.
