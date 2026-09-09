---
feature: flow_routing_identity_chain
domain: flows
one_liner: "Follows an attributed inbound message from sender resolution through channel policy, two-pass routing, identity Stage 2 and auto-install, to the agent, session and recorded verdict."
primary_label: flow
---
# Routing and the Identity Chain

## Purpose

This flow answers two questions about one already-accepted message: **who is the sender**, and **which agent gets it**. It begins where [Message Ingress](message_ingress.md) hands over — a transport has authenticated the payload, an adapter has parsed it into a `ChannelInboundMessage` carrying `sender_email`, `thread_key` and `text`, and the message is attributed to a `ServerChannel` row. Nothing below re-reads a webhook signature or an IMAP mailbox; the sender's verified address is treated as the sender's identity from the first line, and everything after it keys on that.

It ends at one point: an agent and its active environment are selected, a session is resumed or created in the right person's workspace, and a durable verdict is written to `routing_decision`. Between the two sit four separately-owned decisions — the whitelist and account resolution (`ChannelInboundService`), the per-person channel policy (`ChannelPolicyService`), the two-pass ballot (`ChannelRoutingService`), and the identity handoff (`IdentityRoutingService`). [App MCP Server](../application/app_mcp_server/app_mcp_server.md) is documented here as a parallel path rather than a separate story, because its candidate providers and classifier are literally the same functions.

## Participants

| Feature | Role in this flow | Module |
|---|---|---|
| [Server Channels](../application/server_channels/server_channels.md) | Resolves the sender's account, applies the whitelist, dispatches continue-vs-route, and owns every effect (bind, install, ingest, reply) | `backend/app/services/server_channels/channel_inbound_service.py` |
| [Server Channels](../application/server_channels/server_channels.md) | The one place channel policy is resolved — availability, agent scope, pin, identity consent, auto-install | `backend/app/services/server_channels/channel_policy_service.py` |
| [Server Channels](../application/server_channels/server_channels.md) | The pure decision: Pass 1 over owned agents + identities, Pass 2 over the auto-install catalog | `backend/app/services/server_channels/channel_routing_service.py` |
| [Identity Routing](../application/identity_routing/identity_routing.md) | Stage 2 — picks one of the addressed person's agents for this specific caller | `backend/app/services/identity/identity_routing_service.py` |
| [Identity Routing](../application/identity_routing/identity_routing.md) | The six-condition re-verification standing behind every identity grant | `backend/app/services/identity/identity_service.py` |
| [Agent Prompts](../agents/agent_prompts/agent_prompts.md) | Supplies `Agent.router_trigger_prompt` / `example_prompts`, the text the classifier reads | `backend/app/models/agents/agent.py` |
| [Auto Routing Tuning](../application/routing_tuning/routing_tuning.md) | Records candidates, prompts, skips and the terminal outcome; computes the reachability verdict | `backend/app/services/routing/routing_trace.py`, `backend/app/services/routing/routing_reachability_service.py` |
| [Agent Bundles & Installs](../agents/agent_bundles/agent_bundles.md) | Auto-install as a routing outcome — Pass 2's winner becomes a real agent | `backend/app/services/bundles/install_service.py` |
| [Channel Ingestion](../application/agent_sessions/channel_ingestion.md) | Access check, session owner selection, session create/resume, message send | `backend/app/services/sessions/channel_ingestion_service.py` |
| [App MCP Server](../application/app_mcp_server/app_mcp_server.md) | The parallel Stage 1 for MCP clients, over the same two providers and the same classifier | `backend/app/services/app_mcp/app_mcp_routing_service.py` |

## The flow

### 1. Sender → platform account

`ChannelInboundService.process_inbound` (`backend/app/services/server_channels/channel_inbound_service.py`) runs the gates in a fixed order, and the order is load-bearing. It first drops non-message events, then refuses a message with no `sender_email` (`REPLY_DENIED`) and acks-and-drops one with no `thread_key` — nothing can be bound without a thread key.

Redelivery dedup comes next: a binding whose `last_external_message_id` equals this one's is acked, and a *pre-binding* redelivery is caught by an in-process `_seen_recently` check gated on `binding is None` — so a redelivery of a message that failed to process stays a recovery opportunity.

Then the whitelist: `match_email_pattern(inbound.sender_email, channel.email_whitelist)`. It fails closed — a NULL or empty pattern denies everyone, and `"*"` is the only way to admit all verified senders. A denial is audited (`SERVER_CHANNEL_SENDER_DENIED`, throttled) and recorded in the [Channel Debug Monitor](../application/server_channels/channel_debug_monitor.md) buffer.

`ChannelInboundService._resolve_user` then maps the address to a `User`. An inactive account is denied; an unknown sender is denied unless `channel.auto_register_users` is on, in which case `UserService.create_external_user` creates a confirmed, passwordless account and audits `SERVER_CHANNEL_USER_AUTO_REGISTERED`. The channel whitelist is the sole registration gate — `AUTH_WHITELIST_USER_DOMAINS` is deliberately not re-checked, because the transport already verified the address.

### 2. Channel policy — resolved once, per message

`ChannelPolicyService.describe(db, channel, user_id)` (`backend/app/services/server_channels/channel_policy_service.py`) reads four facts off two tables and returns a frozen `ResolvedChannelPolicy`; `resolve()` is the narrower wrapper routing uses. The governing rule is that **absence of a `channel_user_setting` row means "the channel default applies"** — the two populations that will never have one (auto-registered senders, and users created after the channel) are exactly the ones this feature exists for, and no read path may materialize the row.

The resolution produces:

- `is_available` = `channel.enabled` AND access (public visibility, or a `ServerChannelUserGrant`) AND the per-user toggle. `process_inbound` declines here and never schedules routing.
- `agent_scope` — `"all"` | `"list"` | `"none"` via `_normalise_scope`; unknown values degrade to `"none"`, the *visible* failure. `allowed_agent_ids` is `None` when the list is not the mechanism and an empty `frozenset` when it is the mechanism and is empty — not interchangeable.
- `pinned_agent_id` — via `_owned_pin`, which nulls a pin whose agent the sender no longer owns (the FK guarantees existence, never ownership).
- `allow_identity_routing` — the **sender's** consent, read only off their own settings row and **never inherited**. `allow_auto_install` — the channel's admin setting, which a user cannot override.

### 3. Binding dispatch

`ChannelInboundService._get_binding` looks up the `ChannelThreadBinding` for `(channel, thread_key)`. If one exists:

- `binding.user_id != user.id` → declined as `REPLY_THREAD_OWNED`. **Thread ownership is what stops one member of a group space posting into another person's conversation**, and it is unaffected by identity.
- a control command with no attachments → `execute_control_command` (`backend/app/services/server_channels/channel_control_commands.py`), never sent to an agent.
- status `active` → `_continue_thread`, which drains any parked messages first and then ingests.
- status `pending_install` → `_park_message` (an auto-install is still in flight).
- status `failed` → the binding is **deleted** and the message falls through to routing. A failed identity binding therefore self-heals: the next message re-routes over the sender's own agents.

With no binding (or after clearing a failed one), `process_inbound` picks `classification_text` — `inbound.text` when the sender wrote something, a filename-derived string when the message is attachment-only, otherwise the composed `text` — and schedules `ChannelInboundService._route_new_thread` as a background task, carrying the frozen `policy` and `origin=_trace_origin(adapter.channel_type)` (`google_chat` → `server_channel`, `email` → `email`).

### 4. The decision boundary

`_route_new_thread` opens its own session, optionally posts a "working" status notice, and calls `ChannelRoutingService.decide` (`backend/app/services/server_channels/channel_routing_service.py`). `decide` is **pure by call graph, not by flag**: it takes ids, text and a frozen policy — never a `Session` — imports nothing effectful, and returns plain data. `backend/tests/architecture/channel_routing_purity_test.py` executes all four of those facts, which is what makes `POST /api/v1/admin/routing/simulate` safe by construction.

Both passes are offloaded via `ChannelRoutingService.run_in_thread`, because routing ends in a blocking LLM HTTP call on an externally-triggerable path. The Pass-1 thread target `_route_installed_in_thread` opens `RoutingTrace.capture(origin=…, stage=STAGE_PASS_1)` **inside the thread** — the recorder is a `ContextVar`, so a capture opened around the offload would not be visible to it.

### 5. Pass 1 — the ballot

`ChannelRoutingService._route_installed` decides in this order:

1. **Pin.** `policy.pinned_agent_id is not None` → `_route_pinned_agent`, which records `match_method="pinned"`, re-checks ownership (defence in depth over `_owned_pin`), records the single candidate as eligible, and returns. It runs *before* the provider, before `only_one`, and bars Pass 2 — a pin is an instruction, not an inference.
2. **Ballot composition.** `ChannelCandidateProvider.build(db, user.id, policy=policy)` (`backend/app/services/routing/channel_candidate_provider.py`) selects `WHERE Agent.owner_id = :user_id ORDER BY name, id` — the whole set — then filters *in Python* so every exclusion is recorded: `SKIP_NOT_IN_CHANNEL_SCOPE` (via `_in_scope`, an allowlist over `agent_scope`) and `SKIP_NO_TRIGGER_PROMPT`. Then `IdentityCandidateProvider.build(db, user.id, policy=policy)` (`backend/app/services/routing/identity_candidate_provider.py`) appends **one candidate per person**, not per binding, under the namespaced ref `identity:{owner_id}` so a person can never be looked up as an agent. `policy` is keyword-only and required on both: a permissive default is a restriction that stops applying the day someone adds a call site.
3. **Empty ballot** → `OUTCOME_NO_MATCH`, straight to Pass 2. This is the onboarding path.
4. **Exactly one candidate** → `_catalog_ballot` probes the catalog for *availability only*. If `CatalogBallot.offers_an_alternative` is false, `_route_only_candidate` takes that candidate with `match_method="only_one"` and no model call. **A short-circuit is sound only when there is no alternative to choose between** — and a newly auto-registered sender owns exactly one agent the moment Pass 2 onboards them, so the probe is what stops the onboarding message being the last one that could ever reach the catalog. The sole candidate may be an identity, in which case Stage 2 still runs.
5. **Otherwise** → `AgentClassifier.classify(candidates, text)`.

`AgentClassifier` (`backend/app/services/routing/agent_classifier.py`) is the single renderer and parser for all four consumers (channel Pass 1, channel Pass 2, App MCP Stage 1, identity Stage 2). It renders the prompt from each candidate's `trigger_prompt` and `prompt_examples`, records prompt and raw reply on the trace, and returns `None` for every negative outcome — non-JSON, `NONE`, malformed id, provider-cascade failure — recording *which* one it was. It never settles the trace's outcome.

`record_match(MATCH_AI)` is written **before** the guards, so `outcome=no_match, match_method=ai, selected_agent_id=NULL` stays a distinguishable diagnosis. `parse_identity_ref` is checked **before** `uuid.UUID(...)` — an identity ref is not a UUID by design, and parsing first would turn every identity win into "not a UUID". A non-identity winner is re-loaded by id and rejected with `SKIP_FOREIGN_OWNER` if `agent.owner_id != user.id`.

### 6. Stage 2 — inside the person

`ChannelRoutingService._route_identity` opens `routing_trace.stage_scope(STAGE_IDENTITY_STAGE2)` (scoped, not latched, so later Pass-1 records do not land under it) and calls `IdentityRoutingService.route_within_identity(owner_id, caller_user_id, message)`. The message it passes is Stage 1's `transformed_message` when the classifier stripped a routing prefix — otherwise the prefix naming the *person* competes with the wording naming their *agent*.

`IdentityRoutingService._select` (`backend/app/services/identity/identity_routing_service.py`) gets the bindings this caller can reach (`IdentityService.get_active_bindings_for_user`) and records each with its assignment (no assignment for this caller → a `no_assignment` skip, not a silent drop), then:

- **one binding** → `match_method="only_one"`, no model call. This stays a Stage-2 property because Stage 1's ballot cannot hold "this person has exactly one reachable agent" without re-deriving the caller's access for every candidate on it.
- **two or more** → `_ai_classify` over the same `AgentClassifier`.
- **nothing** → `None`.

`match_method` from Stage 2 is therefore exactly `"only_one"` or `"ai"`. Glob matching was removed and `IdentityAgentBinding.message_patterns` was dropped from the schema — it was a second mechanism with silently higher priority than the classifier and no trace explained it.

Back in `_route_identity`, the returned agent is re-loaded and must satisfy `agent.owner_id == owner_id`. What crosses back is an `IdentityGrant` (`backend/app/models/sessions/session_sender.py`) — three UUIDs on a frozen dataclass, a *claim* and never a conclusion.

`decide` then returns immediately: **identity is terminal.** Whether Stage 2 found an agent or nothing, Pass 2 does not run — auto-installing a bundle for somebody who asked to speak to a colleague is not a better answer than "nothing matched".

### 7. Pass 2 — auto-install as a routing outcome

`ChannelRoutingService._catalog_may_run` is the conjunction of four unrelated switches: `include_catalog` (the admin's simulate toggle, always true on a real message), `policy.allow_auto_install`, `policy.agent_scope == "all"`, and `policy.pinned_agent_id is None`. The scope term is a product decision, not the same filter reapplied: an agent arriving from the catalog is out of scope by construction, so the install would succeed and every later thread would dead-end. When Pass 2 is barred, `_record_pass_2_not_run` writes a machine-readable `not_run_code` and a note naming the control to look at.

`_gather_catalog_candidates` requires all four of: the bundle resolves with a published revision, this user has not already installed it, `CatalogService.user_can_install` says yes (list membership is not a grant), and the revision carries a router trigger prompt. Everything else becomes a skip with a reason — `already_installed`, `not_installable`, `no_revision`, `no_trigger_prompt` — because the question this pass answers is "why was this user never offered anything?". `_route_catalog` classifies the survivors and settles `OUTCOME_PARKED_INSTALL`.

Back in `_route_new_thread`, `_install_and_park` calls `InstallService.install_bundle`, audits `SERVER_CHANNEL_AUTO_INSTALL`, upserts a `pending_install` binding, parks the message on it, and tells the sender. `flush_pending_bindings` / `_drain_parked` deliver it once the environment is up.

### 8. Effects — bind, verify, ingest

For an agent decision, `_route_new_thread` persists the trace, records a debug-feed entry carrying `detail.trace_id`, and calls `_upsert_binding` (a lost race goes to `_handle_lost_race` rather than double-binding). Then `_ingest_or_fail` → `ChannelInboundService._ingest`, which:

- enforces `user.id == binding.user_id` at the top of the body;
- rebuilds the grant from the **session row** on a resume (`_resume_identity_grant`, which requires all three identity columns and refuses a partially-stamped row) or takes Stage 2's grant on first contact;
- raises `ChannelDecline` if a grant is present and `policy.allow_identity_routing` is now off — so withdrawing consent stops the conversation it authorised, on the next message;
- stamps `identity_owner_id` / `identity_caller_id` / `identity_binding_id` / `identity_binding_assignment_id` plus `identity_caller_name` into `session_metadata` on create;
- calls `ChannelIngestionService.ingest_inbound_message` with `integration_type=f"channel_{channel.channel_type}"`.

`ChannelIngestionService.assert_access` (`backend/app/services/sessions/channel_ingestion_service.py`) is the authorization. On the `channel_caller` arm, the ordinary case is `agent.owner_id == expected_owner_id == sender.platform_user_id`; anything else needs a grant, and a grant is re-read through `IdentityService.verify_identity_access`, which checks **six** conditions every time: the binding is live and active; the assignment is live, active and enabled; the assignment belongs to that binding; it was issued to this caller; the binding exposes this agent; and `binding.owner_id == agent.owner_id == claimed owner`. Conditions 3–6 are what stop three individually-live rows from three different authorizations being assembled into a fourth that never existed.

`resolve_or_create_session` then resumes (verifying the sender against the existing row) or creates. `_select_session_owner_id` places the session: the sender's own id normally, the identity owner's id only when an `identity_owner_id` override arrives **and** the policy carries a grant whose `owner_id` matches `agent.owner_id`. `SessionService.create_session` binds the session to `agent.active_environment_id` and returns `None` when there is none, surfacing as `NoActiveEnvironmentError`. The message is then sent and streaming kicked off.

### 9. The verdict

`RoutingTraceService.persist` (`backend/app/services/routing/routing_trace_service.py`) opens its **own** short-lived session — a failed diagnostic can never commit or roll back the caller's transaction — and never raises. `RoutingDecisionResult.persist_args` encodes the merge rule once: when Pass 2 ran it is the terminal trace and Pass 1 folds in behind it, otherwise Pass 1 stands alone. One inbound message is one row, on the real path and on simulate alike.

On the **error** path the persist happens *inside* the thread target, before re-raising, because `capture` re-raises unchanged and the caller would otherwise never receive the recorder. Callers must account for that: an exception out of `decide` means a trace has already been written.

`GET /api/v1/admin/routing/traces/{trace_id}` attaches `RoutingReachabilityService.diagnose` — a plain-language verdict computed on the backend from the **projected** public model, so it can never quote a field the message-text gate withheld, and total (any failure returns `CODE_UNAVAILABLE` rather than taking the trace read down). `?expected_agent_id=` takes a *candidate ref*, so `identity:{owner_id}` asks "why was this person not reachable" — parsed before any `db.get(Agent, …)`.

### 10. The App MCP parallel path

`AppMCPRoutingService.route_message` (`backend/app/services/app_mcp/app_mcp_routing_service.py`) resolves the singleton App MCP channel and the caller's policy through the same `ChannelPolicyService`, opens `RoutingTrace.capture(origin=ORIGIN_APP_MCP, stage=STAGE_PASS_1)` (governed by `ROUTING_TRACE_APP_MCP_MODE`), and `_decide` composes **the same two providers** and calls **the same classifier**. It has no Pass 2 — there is no auto-install catalog on this surface — so the shape is Stage 1 + Stage 2, not two passes. `policy.is_available` is not re-checked here; that gate belongs to `app_token_verifier.is_app_mcp_available` (`backend/app/mcp/app_token_verifier.py`).

An identity win goes to `AppMCPRequestHandler._create_identity_session` (`backend/app/services/app_mcp/app_mcp_request_handler.py`), which builds the `IdentityGrant` from the Stage-2 result and calls `ChannelIngestionService.create_identity_session` with `integration_type="identity_mcp"`. On resume, `_try_resume_session` matches on `identity_caller_id` (never `user_id`), then re-checks `IdentityService.check_session_validity` **and** `_check_identity_routing_consent` — which re-resolves the policy rather than reading the settings row.

## Decision points

| Where (file) | Decides on | Outcomes |
|---|---|---|
| `channel_inbound_service.py` `process_inbound` | `channel.email_whitelist` vs `sender_email` | admit / deny (fails closed on empty) |
| `channel_inbound_service.py` `_resolve_user` | existing account, `is_active`, `channel.auto_register_users` | user / auto-created user / deny |
| `channel_policy_service.py` `describe` | channel enabled, grant/visibility, per-user toggle | `is_available` true / false (decline) |
| `channel_policy_service.py` `_normalise_scope` | stored `agent_scope` | `all` / `list` / `none` (unknown → `none`) |
| `channel_inbound_service.py` `process_inbound` | `binding.user_id` vs sender | continue / `REPLY_THREAD_OWNED` |
| `channel_inbound_service.py` `process_inbound` | `binding.status` | continue / park / delete-and-reroute |
| `channel_routing_service.py` `_route_installed` | `policy.pinned_agent_id` | pinned route / normal ballot |
| `channel_candidate_provider.py` `_in_scope` | `agent_scope`, `allowed_agent_ids` | candidate / `not_in_channel_scope` skip |
| `identity_candidate_provider.py` `build` | `policy.allow_identity_routing` | `[]` with **no trace rows** / one candidate per person |
| `identity_candidate_provider.py` `build` | binding + assignment + caller toggle | candidate / `identity_unavailable` skip |
| `channel_routing_service.py` `_catalog_ballot` | catalog offers an alternative | `only_one` short-circuit / classify |
| `agent_classifier.py` `classify` | LLM reply | candidate ref / `None` + recorded parse outcome |
| `channel_routing_service.py` `_route_installed` | `parse_identity_ref(ref)` | Stage 2 handoff / agent lookup |
| `identity_routing_service.py` `_select` | number of reachable bindings | `only_one` / `ai` / `None` |
| `channel_routing_service.py` `_route_identity` | `agent.owner_id == owner_id` | grant / `SKIP_FOREIGN_OWNER` |
| `channel_routing_service.py` `decide` | agent found, identity present, `_catalog_may_run` | return / run Pass 2 |
| `channel_routing_service.py` `_catalog_may_run` | 4-term conjunction | Pass 2 runs / `not_run_code` recorded |
| `channel_inbound_service.py` `_ingest` | grant present AND consent withdrawn | `ChannelDecline` |
| `identity_service.py` `verify_identity_access` | six conditions | `None` (allow) / caller-safe denial |
| `channel_ingestion_service.py` `_select_session_owner_id` | `identity_owner_id` + grant match | sender's space / owner's space |
| `routing_reachability_service.py` `_diagnose` | outcome, candidates, skip reasons, expected ref | one of 16 verdict codes |

## Where to change what

| If you want to … | Edit | Re-check |
|---|---|---|
| change who may send at all | `channel_inbound_service.py` whitelist / `_resolve_user` | auto-registration audit events; the debug-feed `stage` labels |
| add or change a policy field | `channel_policy_service.py` (`ResolvedChannelPolicy` + `describe`) | every consumer of the frozen dataclass — both providers, `_catalog_may_run`, `_ingest`; and `ChannelPolicyView` for the UI |
| change which owned agents are eligible | `channel_candidate_provider.py` | `_catalog_may_run`'s scope term; the `not_in_channel_scope` verdict codes |
| change who may be addressed as a person | `identity_candidate_provider.py` `build` | both consumers (channel `_route_installed` and App MCP `_decide`); the §3.5 trace inversion |
| change the classifier prompt or parsing | `agent_classifier.py` | all four consumers at once; `backend/tests/unit/test_router_classification_agent_name.py` asserts against the rendered prompt |
| change Stage-2 selection | `identity_routing_service.py` | the `IdentityGrant` fields; `verify_identity_access`'s condition 5 |
| change when auto-install may run | `channel_routing_service.py` `_catalog_may_run` | the `only_one` probe (it reads the same function), and the three `PASS_2_*_NOTE` strings |
| change identity session ownership | `channel_ingestion_service.py` `_select_session_owner_id` | `assert_access`'s two grant arms; the `channel_` prefix gate on reply delivery |
| add a trace field | `routing_trace.py` (`StageTrace`, `SAFE_STAGE_FIELDS`) | `routing_trace_service.py`'s projection, used on write **and** read; the frontend stage views |
| add a verdict code | `routing_reachability_service.py` | `backend/tests/api/routing/routing_reachability_verdict_test.py` — each code carries a matching test |

## Invariants and traps

- **`decide` holds no caller session, imports nothing effectful, and returns plain data.** Four structural facts, all executed by `backend/tests/architecture/channel_routing_purity_test.py`, which is parameterized over the Stage-2 module too — it runs inside the same boundary and inherits all four.
- **An `IdentityGrant` is a claim, never a conclusion.** The decision and the session are separated by a thread hop (and possibly an install wait); `assert_access` re-reads all six conditions on **every** message.
- **`ChannelThreadBinding.user_id` is the sender; `session.user_id` is the identity owner.** They diverge on an identity thread and answer different questions — *whose thread is this* versus *whose workspace is answering*.
- **`integration_type` must stay `channel_<type>` on an identity-routed channel session.** `ChannelOutboundService._resolve_channel_session` gates reply delivery on that prefix, so a session stamped `identity_mcp` there would route correctly, run correctly, and never deliver a word.
- **Identity owners the sender could have reached, with the consent switch off, leave no trace rows at all** — not even skips. The one deliberate inversion of "record every rejected candidate with a reason", enforced inside `IdentityCandidateProvider.build`: recording them would let an externally-triggerable trace enumerate other people's identities. Do not "fix" it by filtering afterwards.
- **A `SKIP_IDENTITY_UNAVAILABLE` row is itself evidence** that the sender's channel switch was already on.
- **Empty whitelist denies everyone; unknown `agent_scope` admits nothing.** Both degrade toward the visible failure — a trace full of reasons — not an over-broad ballot that looks like it worked.
- **`match_method` survives a `no_match`.** `outcome=no_match, match_method=ai, selected_agent_id=NULL` means the classifier picked something and a guard rejected it, which is a different diagnosis from "nothing matched".
- **Two vocabulary values have no live producer.** `MATCH_PATTERN` — glob matching was deleted from both Stage 2 and App MCP Stage 1 — and `SKIP_IDENTITY_ROUTE`, the rejection reason of the deleted downstream-filter branch. Both survive as rendering constants for rows captured before those deletions.
- **`origin` splits `email` from `server_channel`.** Rows written before that split carry `server_channel` for email; anything counting real traffic must name both. `identity` is declared and never emitted — identity is a *stage* inside another origin's decision.
- **An exception out of `decide` means a trace was already persisted.** The thread targets write their own `outcome=error` row before re-raising, because `capture` re-raises unchanged; `RoutingTuningService.simulate` points the admin at that row rather than reporting a bare failure over the top of it.
- **`RoutingDecision.stages` is JSONB and needs plain assignment** — `row.stages.append(...)` is not dirty-tracked and a commit drops it.
- **The known regression this flow was rebuilt around:** Pass 1 used to borrow App MCP's candidate set and filter foreign agents and identity routes *downstream of the classifier*. The model spent its decision on a route the filter then rejected, and nothing re-classified over the survivors — a near miss became a total failure. The fix is structural: providers compose, surfaces do not borrow, and ineligible candidates are removed **before** the classifier.

## See also

- [Message Ingress](message_ingress.md) — where this flow's input comes from
- [Server Channels](../application/server_channels/server_channels.md) · [tech](../application/server_channels/server_channels_tech.md)
- [Identity Routing](../application/identity_routing/identity_routing.md) · [tech](../application/identity_routing/identity_routing_tech.md)
- [Auto Routing Tuning](../application/routing_tuning/routing_tuning.md) · [tech](../application/routing_tuning/routing_tuning_tech.md)
- [App MCP Server](../application/app_mcp_server/app_mcp_server.md) · [tech](../application/app_mcp_server/app_mcp_server_tech.md)
- [Channel Ingestion](../application/agent_sessions/channel_ingestion.md) · [tech](../application/agent_sessions/channel_ingestion_tech.md)
- [Channel Debug Monitor](../application/server_channels/channel_debug_monitor.md)
- [Agent Bundles & Installs](../agents/agent_bundles/agent_bundles.md)
- [Agent Prompts](../agents/agent_prompts/agent_prompts.md)
