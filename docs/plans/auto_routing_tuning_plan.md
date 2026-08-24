# Auto Routing Tuning — Implementation Plan

**Feature name:** `auto-routing-tuning`
**Status:** Draft for implementation
**Parent feature:** [Server Channels](../application/server_channels/server_channels.md) (routing is shared with App MCP Server + Identity MCP Server)
**Admin surface:** `/admin/server-configuration#channels` — new "Auto Routing Tuning" card

---

## 1. Overview

Channel routing makes an LLM-mediated decision per new thread, and today the only trace of it is a
one-line `no_match` in a **process-local, in-memory** ring buffer (`ChannelDebugBuffer`) plus INFO
logs. When an admin sees "the bot didn't find my agent", there is no way to answer *why*: which
agents were even candidates, what prompt the classifier saw, which providers/models were tried,
what the model actually replied.

This feature makes every routing decision **observable, durable, reproducible, and advisory**:

- A **routing trace** captured at the router chokepoint — candidates (including *rejected* ones with
  a reason), rendered prompt, provider/model attempts, raw LLM response, verdict, latency, optional
  confidence.
- **Durable persistence** with a retention window, so tuning can look back past a backend restart
  and across workers (the in-memory buffer can do neither).
- A **simulate / replay** tool: run a message through routing with **no side effects** (no binding,
  no session, no install, no outbound reply), then re-run it after a change and diff.
- An **admin tuning card** that diagnoses no-matches in plain language and drafts a recommendation
  for the agent's owner.

**Explicitly out of scope — and this is a hard boundary.** The admin surface is *read-only with
respect to agents*. It never edits another user's agent, trigger prompt, or bundle. When a foreign
agent routes badly the output is a **copyable recommendation for its owner**, nothing more. The fix
path stays: tune your own agent, or update the bundle and republish.

```
inbound message ──▶ RoutingTrace.capture(origin=…) ──┐
                     │                                │
                     │  effective routes ─────────────┤ candidates + skip_reasons
                     │  pattern match ────────────────┤ match_method
                     │  AI classify ──────────────────┤ prompt, raw response, confidence
                     │  provider cascade ─────────────┤ LLMAttempt per provider tried
                     ▼                                ▼
              routing decision                 routing_decision row (JSONB stages)
                     │                                │
                     ├──▶ ChannelDebugBuffer (live)   ├──▶ GET /admin/routing/traces
                     │     detail.trace_id ───────────┘
                     └──▶ bind + ingest (real path only; simulate stops here)
```

---

## 2. Why now — two live bugs this work surfaces

Both were found while designing this and both independently explain "it didn't find my agent".
They are **in scope**, but see the phasing note: the second changes routing behaviour and must land
*after* traces exist so the delta is measurable.

### Bug 1 — `prompt_examples` never reaches the LLM

`prompt_examples` is validated (2000 chars / 10 lines), stored on `AppAgentRoute` and
`IdentityAgentBinding`, documented as a routing aid, and passed into `available_agents` by
`app_mcp_routing_service.py:269` — but `app_agent_router.py:58` builds `agents_section` from
`id` / `name` / `trigger_prompt` only. The field is silently dropped before the prompt is rendered.

### Bug 2 — a standalone agent is invisible to routing

`AppAgentRouteService._create_auto_route_for_agent` (`app_agent_route_service.py:698`) returns
`None` when `agent.bundle_uuid IS NULL`. A non-bundle agent therefore has no `AppAgentRoute`, is
absent from `get_effective_routes_for_user`, and the classifier never sees it. Pass 2 does not
rescue it either — Pass 2 classifies *bundles on the auto-install list*, not agents.

This is **not** fixed by changing the auto-route rule (standalone agents are deliberately
owner-managed). It is fixed by *diagnosing it clearly*: the tuning card's reachability verdict must
say, in words, "this agent is not a candidate because it is not a bundle install and has no App MCP
route — add one from its Integrations tab."

### Related: a deferred gap this closes

`channel_inbound_service.py:927` documents a known gap — `[Stage1]` (`app_mcp_routing_service.py`)
and `[AIRouter]` (`app_agent_router.py`) log **external users' message text at INFO**. Traces
replace what those lines were for, so this work downgrades them to `debug`.

---

## 3. Architecture

### Components

| Component | Location | Responsibility |
|---|---|---|
| Trace recorder | `backend/app/services/routing/routing_trace.py` (new pkg) | `RoutingTrace` span object + `ContextVar`, `CandidateTrace`, `LLMAttempt`, `StageTrace` |
| Trace persistence | `backend/app/services/routing/routing_trace_service.py` | Persist a closed trace; list/get for admin; purge |
| Model | `backend/app/models/routing/routing_decision.py` (new domain) | `RoutingDecision` table + `RoutingDecisionPublic` / `RoutingDecisionSummary` |
| Classifier | `backend/app/services/routing/agent_classifier.py` | Unified prompt build + parse + trace emission (replaces 3 near-copies) |
| Decide split | `backend/app/services/server_channels/channel_routing_service.py` | Pure `decide()` — routing without effects; used by the real path *and* simulate |
| Retention | `backend/app/services/routing/routing_trace_scheduler.py` | TESTING-gated hourly purge |
| Routes | `backend/app/api/routes/admin_routing.py` | Superuser: list/get traces, simulate, replay, recommendation draft |
| Admin UI | `frontend/src/components/Admin/ServerChannels/AutoRoutingTuningCard.tsx` (+ children) | The tuning card on `#channels` |

### The four routing consumers (one classifier underneath)

| Caller | Entry | Classifies over |
|---|---|---|
| App MCP request handler | `app_mcp_request_handler.py:203` → `AppMCPRoutingService.route_message` | effective routes for user |
| Identity Stage 2 | `identity_routing_service.py:243` → `app_agent_router.route_to_agent` | identity bindings |
| Channel Pass 1 | `channel_inbound_service.py:936` → `AppMCPRoutingService.route_message` | sender's own installs (then owner-filtered) |
| Channel Pass 2 | `channel_inbound_service.py:1035` → `AIFunctionsService.route_to_agent` | `ServerAutoInstallBundle` list |

All four funnel into `app_agent_router.route_to_agent` → `ProviderManager.generate_content`.
**Instrument there, not in the channel service.**

### Reused existing systems (do NOT reinvent)

- **`ChannelDebugBuffer`** (`channel_debug_buffer.py`) — keep it as the *live* view. It gains a
  `trace_id` in `detail` so a live row links to its durable trace. Its never-raises discipline,
  clamping, and consecutive-collapse behaviour are the model for the recorder.
- **`ProviderResponse`** (`providers/base.py:34`) already carries `provider_name` / `model` /
  `usage`. `ProviderManager.generate_content` already accumulates `errors: list[tuple[str, str]]`
  (`provider_manager.py:169`) and **discards it on success** — that list is "what models we tried".
- **`AppAgentRouteService._jaccard_similarity` / `_tokens_for_similarity`** (`:750-775`,
  `SIMILARITY_THRESHOLD = 0.45`) — already used for install-time route-conflict detection. Reuse
  verbatim for "closest trigger prompt was X (0.31)".
- **`AIFunctionsService.generate_router_trigger_prompt`** (`:650`) — the advisory draft.
- **`SecurityEvent`** (`models/events/security_event.py`) — durable audit for simulate/replay runs
  (precedent: admin test-send is already audited in `server_channels.py`).
- **`services/common/rate_limiter.py`** — rate-limit simulate (real LLM spend per click).
- **Scheduler + TESTING gate** — `channel_pending_scheduler.py` / `file_cleanup_scheduler.py`.
- **`GET /users/search` + `UserAllowlistPicker`** — the simulate form's user picker (needs
  `fallbackLabel` + an `enabled` gate; see the user-search-picker conventions).

---

## 4. Data model

### `routing_decision` (new table — migration required)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `created_at` | timestamptz | |
| `origin` | str | `server_channel` \| `app_mcp` \| `identity` \| `simulate` |
| `channel_id` | FK `server_channel` `ondelete=CASCADE`, nullable | |
| `user_id` | FK `user` `ondelete=SET NULL`, nullable | the *sender* being routed for |
| `actor_user_id` | FK `user` `ondelete=SET NULL`, nullable | the admin, on a simulate/replay |
| `thread_key` | str, nullable | |
| `message_text` | Text, nullable | clamped; gated by `ROUTING_TRACE_STORE_MESSAGE_TEXT` |
| `message_sha256` | str, indexed | **always** present — replay/dedupe key when text is off |
| `outcome` | str | `routed` \| `no_match` \| `error` \| `parked_install` |
| `match_method` | str, nullable | `pattern` \| `ai` \| `only_one`. **"How the last stage matched"**, not "how the decision was reached" — it deliberately survives a `no_match` (a stage matched, the ownership filter then rejected it). Diagnostically useful; Phase 2's column docs must carry this wording. |
| `selected_agent_id` | FK `agent` `SET NULL`, nullable | |
| `selected_bundle_uuid` | FK `agent_bundle` `SET NULL`, nullable | |
| `confidence` | float, nullable | |
| `latency_ms` | int | |
| `stages` | JSONB | candidates + LLM attempts + prompt + raw response |
| `error` | Text, nullable | |

Indexes: `(created_at DESC)`, `(channel_id, created_at DESC)`, `(user_id, created_at DESC)`,
`(message_sha256)`.

**Why `stages` is JSONB and not child tables:** it is read whole, never queried by inner field, and
its shape will change as the router does. Child tables would fossilise today's two-pass structure.
Precedent: `InputTask.refinement_history`, `Environment.config`.

### Trace dataclasses

```python
@dataclass
class CandidateTrace:          # EVERY considered candidate, including rejected ones
    kind: str                  # "agent" | "bundle"
    ref_id: str
    name: str
    owner_email: str | None
    source: str                # "admin" | "user" | "identity" | "catalog"
    trigger_prompt: str        # clamped
    prompt_examples: str | None
    eligible: bool
    skip_reason: str | None    # already_installed | not_installable | no_trigger_prompt
                               # | identity_route | foreign_owner | route_inactive

@dataclass
class LLMAttempt:
    provider: str
    model: str | None
    ok: bool
    error: str | None
    latency_ms: int

@dataclass
class StageTrace:              # pass_1 | pass_2 | identity_stage2
    stage: str
    candidates: list[CandidateTrace]
    match_method: str | None
    matched_pattern: str | None
    prompt: str | None         # rendered classifier prompt (clamped)
    raw_response: str | None   # clamped
    llm_attempts: list[LLMAttempt]
    confidence: float | None
    reason: str | None
    runner_up_id: str | None
```

**The single highest-value rule in this plan:** `candidates` must include *excluded* candidates with
a `skip_reason`. A trace that lists only the finalists cannot diagnose the failure mode that
actually bites (the expected agent was never a candidate at all).

### Settings (`core/config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `ROUTING_TRACE_ENABLED` | `True` | master switch |
| `ROUTING_TRACE_STORE_MESSAGE_TEXT` | `True` | see §7 |
| `ROUTING_TRACE_RETENTION_DAYS` | `14` | purge window. **Must be `>= 1`.** `-1` is the *only* spelling of "keep forever" (the escape hatch for a debugging session that must outlive the window). `0` and all other negatives are **rejected at settings validation**, naming `-1` in the error. Rationale: §7's case for storing message text is that it changes the *duration* of exposure, not the exposure class — a value that reads as "no retention" but means "unbounded retention" inverts exactly that property, and fails toward keeping *more* external users' text. Documentation does not reach the operator who sets `0` meaning "don't keep this". |
| `ROUTING_TRACE_TEXT_MAX_CHARS` | `2_000` | match `SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS` |
| ~~`ROUTING_TRACE_APP_MCP_MODE`~~ | *(removed — verified absent from `config.py` and `_should_store_text`; only historical comments remain)* | **Removed until the origin exists.** Applied in the Phase 2 fix pass. `capture()` is opened only at the two `origin=server_channel` sites, so this setting was unreachable: an operator setting it to `off` would believe they had disabled capture that was never running — a false assurance, §11a Rule 1 in a different guise. App MCP / identity / simulate capture is deferred; reintroduce this setting **in the same change that adds the origin**, not before. The rationale it encoded still stands and still applies then: App MCP routes *every* message, not just thread openings, so it must default to metadata-only. |
| `ROUTING_SIMULATE_RATE_LIMIT_PER_MIN` | `10` | per admin |

---

## 5. The recorder — mechanics and the one subtle bit

`RoutingTrace.capture(origin=…, user_id=…, channel_id=…, thread_key=…)` is a context manager that
sets a `ContextVar` holding a **mutable** recorder. Instrumentation points call
`RoutingTrace.current()` and **no-op when there is no active capture** — so the four consumers opt
in and *no function signature changes*.

Instrumentation points:

- `AppAgentRouteService.get_effective_routes_for_user` → candidate set with `source`
  (this replaces the `[EffectiveRoutes]` INFO spam).
- `AppMCPRoutingService._try_pattern_match` / `_ai_classify` → `match_method`, `matched_pattern`.
- `app_agent_router.route_to_agent` → rendered prompt, raw response, parse outcome.
- `ProviderManager.generate_content` → one `LLMAttempt` per provider tried, **success and failure**.
- `ChannelInboundService._route_installed` → the identity and foreign-owner rejections as
  `skip_reason`s (currently `logger.info` / `logger.warning` only).
- `ChannelInboundService._route_catalog` → the four `continue` branches (`:993`, `:1006`,
  `:1016`, `:1021`) become recorded skips instead of silent drops.

**Concurrency (read this before implementing).** Channel routing runs its LLM call in a worker
thread via `anyio.to_thread.run_sync` (`channel_inbound_service.py:720`). anyio propagates a *copy*
of the caller's context into the worker, and because the ContextVar holds a mutable object,
mutations inside the thread are visible to the caller. That works — but **open the capture inside
the thread target** (`_route_installed_in_thread` / `_route_catalog_in_thread`) and return
`(id, trace)`. This keeps the lifetime unambiguous and honours the existing rule at `_in_thread`:
the thread target owns its own session and returns plain values, never closes over the caller's.
The recorder takes a lock on append. **This needs an explicit unit test** — a future refactor will
otherwise break capture silently.

**Never break the pipeline.** Every recorder entry point swallows its own errors, exactly like
`ChannelDebugBuffer.record`. Note the same trap documented there: the guard protects the
*recording*, not the caller's argument expressions — keep summary/f-string arguments to attributes
you are certain exist, or an `AttributeError` lands in the broad `except` that abandons the install.

---

## 6. Simulate & replay — the actual tuning tool

### Split `decide()` from its effects

Extract from `ChannelInboundService._route_new_thread`:

```python
ChannelRoutingService.decide(db, user, text, include_catalog: bool) -> RoutingDecisionResult
# pure: returns (agent | bundle | None) + trace. No binding, no session, no install, no reply.
```

`_route_new_thread` becomes `decide()` → bind → ingest. Simulate calls only `decide()`. This is the
same `decide()`-purity shape used in prompt-sync reconciliation, and it is what makes a
side-effect-free simulate safe *by construction* rather than by a flag threaded through 200 lines.

### Routes (all superuser-only, all audited)

| Route | Purpose |
|---|---|
| `GET /api/v1/admin/routing/traces` | paginated list; filter by `channel_id`, `origin`, `outcome`, `user_id` |
| `GET /api/v1/admin/routing/traces/{id}` | full trace with stages |
| `POST /api/v1/admin/routing/simulate` | `{message, as_user_id, include_catalog}` → full trace inline, persisted with `origin="simulate"`, `actor_user_id=<admin>` |
| `POST /api/v1/admin/routing/traces/{id}/replay` | re-run the stored message against *current* state; return the new trace **and a diff** vs the original |
| `POST /api/v1/admin/routing/traces/{id}/recommendation` | `generate_router_trigger_prompt` over the failed message + current prompt → copyable draft. **Writes nothing.** |
| `DELETE /api/v1/admin/routing/traces` | clear (mirrors the debug-events clear) |

**Security.** Simulate reveals which agents a given user has installed — real information about
another account. Superuser-only, rate-limited, and each run writes a `SecurityEvent`
(`ROUTING_SIMULATE_RUN`, new constant — `event_type` is a free-form `str`, no migration needed).
The message body is **not** included in the security event (precedent: admin test-send).

**Projection discipline.** A trace exposes `name`, `owner_email`, `trigger_prompt`,
`prompt_examples`, `source` for a candidate — **nothing else** about a foreign agent. Separately,
`AutoInstallBundlePublic` today exposes only `has_trigger_prompt: bool`; the tuning card needs the
text, so widen it deliberately to `router_trigger_prompt: str | None` and say so in the docs. Watch
for the projection trap: adding the field to the model is not enough if the `_to_public` builder
does not populate it.

---

## 7. The message-text decision (settled)

The Server Channels feature deliberately keeps inbound text out of the database
(`channel_debug_buffer.py:1-30`). A routing trace without the message is close to useless for
tuning. **Decision: store it — clamped, TTL'd, superuser-only, behind
`ROUTING_TRACE_STORE_MESSAGE_TEXT` (default `True`).**

Rationale: the debug buffer already shows that exact text to that exact audience, so this changes
the *duration* (minutes → 14 days), not the exposure class; and a superuser can read the resulting
session's messages through the platform anyway. With the flag off, `message_sha256` + candidate set
+ verdict still answer "which agents were even considered" — which is the diagnosis that matters
most. When `origin="app_mcp"` capture is eventually added it must default to metadata-only,
because App MCP routes every message, not just thread openings — but **no such capture exists yet**
and the setting that expressed it has been removed (§4). This paragraph describes a requirement on
that future change, not current behaviour.

Update `channel_debug_buffer.py`'s module docstring and the Server Channels docs so the
"never persisted" claim is not left standing where it is no longer true.

**`ROUTING_TRACE_STORE_MESSAGE_TEXT` is enforced by an allowlist, not an inventory.** When the gate
is off, the stage payload is projected down to explicitly-named safe fields on both the write and the
read path; anything not on the list is withheld by default. `message_text` is gated alongside it.

This replaced per-field enumeration after enumeration failed three times running — `stages[].prompt`,
`stages[].raw_response`, then `llm_attempts[].error` were each found carrying sender text *after* a
notice had asserted the gate was complete. Sender text is a **taint that propagates**; enumeration is
structurally one field behind it, and every iteration ships an operator-facing claim that decays.
An allowlist inverts the question from *"did we remember every field"* — unanswerable — to *"is this
field safe"*, which the person adding a field can answer when they add it. **Do not describe the gate
by listing fields, in code comments, docs, or the operator notice: describe the mechanism.** A list is
a promise with an expiry date.

**Two kinds of false claim, needing two different defences.** The inventories above — the gated-fields
comment, the clamp-fields list, the `ROUTING_TRACE_TEXT_MAX_CHARS` enumeration — were **true when
written and decayed**. Against decay, describe the mechanism instead of the contents: a mechanism
does not fall behind. But a claim can also be **born false** — `channel_routing_service`'s docstring
asserted four structural facts in the same commit as a test enforcing three, and `clamp()` called
itself total on the day it was written. Mechanism wording does not help there, because the claim was
never true to begin with. Against born-false, the defence is to **write the claim and its enforcement
in the same change, and to treat any guarantee stated in prose as unverified until something executes
it.** Every totality claim in `routing_trace.py` is now pinned by a test for exactly this reason.

Fields admitted deliberately: `candidates[].trigger_prompt` and `candidates[].prompt_examples` are
the *agent owner's* configuration, not sender-derived, already visible to a superuser through normal
platform surfaces — and §9's Jaccard near-miss verdict needs the trigger prompt. Withholding them
would degrade a diagnosis that has nothing to do with sender privacy.

The original three-field framing: The last two carry the sender's
text too — `raw_response` holds the classifier's JSON, which by the router prompt's own contract
contains `"message": "<core task>"`, a rewrite of what the sender wrote, short enough that the clamp
never touches it. A projection-only scrub would hide that from the API while leaving it in the
database for the full retention window, breaking the promise §7 makes, the promise `config.py`
makes, and the promise `MESSAGE_TEXT_HIDDEN_NOTICE` makes to an operator's face.

**Write-gating is also what makes the property structural rather than accidental.** `stages[].prompt`
happens not to contain the message today only because `app_agent_router_prompt.md` (2332 bytes)
overruns `TRACE_TEXT_MAX_CHARS` (2000) before `## User Message` is appended. Trim that template below
~1900 chars — an ordinary prompt edit, which §8 explicitly plans — and the field silently becomes a
full-text leak with no code change and nothing to review. A privacy property must not rest on the
byte length of a markdown file. Once both fields are write-gated, template length cannot create a
leak in either direction: gate on means sender text is stored anyway, gate off means these fields
are not written at all.

The cost is accepted: with the gate off a trace loses its prompt and raw response *at rest*, not
merely at read. `message_text` already makes exactly that trade, and one rule to reason about beats
two.

**`ROUTING_TRACE_STORE_MESSAGE_TEXT` is a read gate as well as a write gate.** When it is off, the
projection **omits `message_text` from rows already written** — it does not merely stop capturing
new ones. An operator flipping this flag under privacy pressure means "stop showing me this text",
not "stop appending to the pile"; a flag that leaves up to `ROUTING_TRACE_RETENTION_DAYS` of text
served from the admin API does not mean what its name says. Same principle as the retention
sentinel: the safe-sounding action must actually be safe.

It **hides, it does not erase.** The rows keep their text until retention expires them. That
limitation must be stated next to the setting and in the admin UI's empty state, naming the two
things that do erase — waiting out `ROUTING_TRACE_RETENTION_DAYS`, or the trace-clear endpoint.
Deliberately *not* purging on flag-flip: an accidental toggle would irreversibly destroy diagnostic
data, and a privacy control whose misfire is unrecoverable is its own hazard.

---

## 8. Classifier unification + confidence

### One classifier

`app_mcp_routing_service._ai_classify`, `identity_routing_service._ai_classify`, and
`channel_inbound_service._route_catalog` each hand-build candidate dicts. Unify behind:

```python
AgentClassifier.classify(candidates: list[Candidate], message: str) -> ClassificationResult
```

owning prompt rendering (**including `prompt_examples`** — Bug 1), parsing, and trace emission.
Fixes the dropped-field bug once instead of three times.

### Confidence & reasoning

Extend `prompts/app_agent_router_prompt.md` to request:

```json
{"agent_id": "<uuid>", "message": "<core task>", "confidence": 0.0,
 "reason": "...", "runner_up": "<uuid>|NONE"}
```

Parse all three **defensively — absent ⇒ `None`, never a parse failure.** Local and small models
routinely ignore added fields; a stricter parse would turn a tuning feature into an outage.

**Record only. Do not gate routing on confidence in this pass.** Once traces exist, a
`SERVER_CHANNEL_MIN_ROUTING_CONFIDENCE` that converts a weak match into "Did you mean X?" becomes a
data-backed decision instead of a guess.

---

## 9. Admin UI — "Auto Routing Tuning" card

Third card on `/admin/server-configuration#channels`, beside `ServerChannelsCard` and
`AutoInstallAgentsCard`.

- **Recent decisions table** — time, origin, sender, message, outcome badge, chosen agent,
  confidence, provider/model, latency. Filter by channel and outcome (`no_match` is the interesting
  one). Unknown `outcome` / `origin` values must still render (the debug dialog's badge convention).
- **Expanded row** — candidate table (name · owner · source · trigger prompt · eligible/skip-reason ·
  ✓chosen), LLM attempts (provider → model → ok/error/ms), raw LLM response, and the rendered prompt
  behind a disclosure.
- **No-match diagnosis** — Jaccard-ranked near-misses ("closest: *Equation Assistant* 0.31") plus a
  plain-language **reachability verdict**, e.g. *"This user has 3 effective routes; the agent you
  expected is not among them because it is not a bundle install and has no App MCP route."* That
  sentence is the whole feature for the motivating case.
- **Try a message** — message box + user picker + include-catalog toggle → the same trace view.
  Answers "is my local LLM broken?" in one click: the provider cascade and raw output are right there.
- **Re-run** on a stored trace, showing a before/after diff.
- **Draft a recommendation** — copyable improved trigger prompt for the owner. Read-only; it never
  writes another user's agent. The UI copy must make that boundary explicit.

Every state needs an `isError` branch — a missing one renders a lying empty state ("no traces")
when the request actually failed.

---

## 10. Phasing

Phases are ordered so the behaviour-changing work lands *after* observability exists, and so each
phase is independently reviewable.

### Phase 0 — confirm the motivating bug (diagnostic, no code)

With Docker up:

```sql
SELECT a.id, a.name, a.bundle_uuid, a.router_trigger_prompt IS NOT NULL AS has_prompt,
       r.id AS route_id, r.is_active, r.channel_app_mcp, r.is_auto_managed
FROM agent a LEFT JOIN app_agent_route r ON r.agent_id = a.id
WHERE a.id = 'f0506e24-3740-4fe3-a28e-d895b92b2ea6';
```

No route row ⇒ Bug 2 confirmed, and the reachability verdict is the headline feature. Record the
finding in the phase notes; it is the acceptance case for Phase 4.

### Phase 1 — recorder + instrumentation (no persistence)

- `routing_trace.py`: `RoutingTrace`, `CandidateTrace`, `LLMAttempt`, `StageTrace`, `ContextVar`.
- `ProviderResponse.attempts: list[ProviderAttempt]` (additive, default empty) —
  `ProviderManager.generate_content` stops discarding its `errors` list. Benefits every AI function.
- Instrument the six points in §5.
- Downgrade the message-text INFO logs in `app_mcp_routing_service.py` and `app_agent_router.py` to
  `debug` — closes the deferred gap at `channel_inbound_service.py:927`.
- Surface the trace into the **existing** `ChannelDebugDialog` (`detail.trace_id` + a richer
  `no_match` summary) to validate the shape before committing to a schema.
- Unit tests: recorder no-ops without a capture; capture survives `anyio.to_thread.run_sync`;
  recorder never raises.

### Phase 2 — persistence + admin read API

- `RoutingDecision` model + alembic migration + settings.
- `RoutingTraceService.persist/list/get/purge`; `routing_trace_scheduler` (TESTING-gated, hourly),
  registered in `main.py` alongside the other schedulers.
- `GET /admin/routing/traces` (+ `/{id}`), `DELETE`. Regenerate the frontend client.
- Update `channel_debug_buffer.py`'s docstring and the Server Channels docs per §7.
- API tests: superuser-only (403 for non-superuser), retention purge, text-gating flag both ways.

### Phase 3 — `decide()` split, simulate & replay

- `ChannelRoutingService.decide()`; `_route_new_thread` refactored to `decide()` → bind → ingest.
  **Regression scope: `tests/api/server_channels/` in full** — this touches the routing core.
- `POST /admin/routing/simulate`, `/traces/{id}/replay`, `/traces/{id}/recommendation`.
- **No `caplog`-based assertion may serve as a no-side-effects proof.** Absence of a side effect is
  asserted against the database, the binding table, the session list, the outbound queue — never
  against a log. `caplog` is vacuous in this suite once Alembic's `fileConfig` has disabled the app
  loggers, and the *negative* form (`assert x not in caplog.text`) passes forever against an empty
  string while reading in review as a careful absence proof. A no-side-effects assertion is exactly
  the shape that invites the vacuous form.
- Rate limiter + `ROUTING_SIMULATE_RUN` security event.
- API tests: simulate creates **no** binding / session / install / outbound reply (assert each
  explicitly — this is the safety property of the whole phase); replay diff; rate limit; audit row.

### Phase 4 — the tuning card

- `AutoRoutingTuningCard.tsx` + trace detail / candidate table / simulate form children.
- Jaccard near-miss ranking and the reachability verdict (backend-computed, so the wording is
  testable and lives with the rules it describes).
- `AutoInstallBundlePublic.router_trigger_prompt` widening.
- `npx tsc --noEmit` scoped to the new components.

### Phase 5 — classifier unification + confidence (behaviour-changing)

- `AgentClassifier`; the three `_ai_classify` copies collapse into it.
- **Render `prompt_examples` into the prompt** (Bug 1). This *changes routing outcomes* for existing
  App MCP users — land it separately, and use Phase 2 traces to measure before/after.
- `confidence` / `reason` / `runner_up` in the prompt template and the defensive parse.
  **Before editing `app_agent_router_prompt.md`, read §7's write-gate paragraph.** That file's length
  was once load-bearing for a privacy property: at 2332 bytes it overran `TRACE_TEXT_MAX_CHARS`
  (2000), which was the only reason `stages[].prompt` did not contain the sender's message. The
  write-gate fix removed that dependency deliberately, so trimming the template is now safe — but
  if anyone ever reverts to gating on the read path only, it becomes load-bearing again and this
  edit silently reintroduces a full-text leak.
- **`stages[].reason` must move OFF the allowlist in this phase.** It is allowlisted today only
  because it holds our own literals. The moment §8 pipes the *model's* `reason` into it, it becomes a
  rewrite of the sender's message — exactly like `raw_response` — and stops being safe. This is the
  precise case the allowlist exists for: a field that is safe now, stops being safe later, and where
  the default must be that someone has to think. Move it off in the same change that starts
  populating it from the model.
- **Identity Stage-2 candidate capture (deferred from Phase 1).** `identity_stage2` currently records a
  prompt and a raw response with **zero candidates** — `IdentityRoutingService` builds its binding list
  inline and was left uninstrumented in Phase 1. This is a *documented deferral, not a recorder bug*:
  anyone reading that shape while building the Phase 4 card will reasonably assume the recorder is
  broken. Close it here, when the classifier is unified and the candidate list has one builder.
- Regression scope: `tests/api/app_mcp/`, `tests/api/server_channels/`, identity routing tests.

### Phase 6 — deferred, NOT in this pass

A curated eval set — `(message, expected_agent)` pairs harvested from traces, replayed as a batch to
score a trigger-prompt change before it ships. This is the "improve routing automatically" endgame
and is a feature in its own right. Phases 1–5 are its prerequisite; note it in the docs as a
follow-up rather than building it here.

---

## 11. Testing

Backend tests are API-only and scenario-based — read `backend/tests/README.md` and
`backend/tests/api/server_channels/README.md` before writing any.

- New group: `backend/tests/api/routing/` (traces, simulate, replay, access control).
- Extend `backend/tests/api/server_channels/server_channels_routing_test.py` for the `decide()` split.
- Unit: `backend/tests/unit/test_routing_trace.py` (recorder semantics + the thread-propagation case).
- The TESTING flag must gate `routing_trace_scheduler` like every other scheduler.

## 11a. Two design rules this feature established

Both were learned the expensive way during implementation. They are rules, not observations —
apply them to every remaining phase and record them in the feature docs.

### Rule 1 — the dangerous state must not be able to look routine

A configuration or display that makes the *hazardous* case indistinguishable from the ordinary one
will be misread, and documentation does not reach the person misreading it. Two instances:

- `ROUTING_TRACE_RETENTION_DAYS = 0` originally meant *keep forever*. Now `>= 1` or the explicit
  `-1` sentinel; `0` is rejected, and the error names the settings that express "store nothing".
- The purge scheduler logged `-1` as "retention -1 days" — the single configuration that keeps
  external senders' message text indefinitely, rendered as a normal number in startup output. Now
  `retention DISABLED (keeping routing traces forever)`.

Third instance, and the first found in the **read path** rather than in configuration: a routing
pass that ran and found nothing was indistinguishable from a pass that never ran. `capture()` did
not materialise its stage, and the entire terminal-verdict vocabulary (`record_outcome`,
`record_error`, `finish`, `note_match_method`) leaves `stages == []`. `_route_catalog` short-circuits
on an empty `ServerAutoInstallBundle` table — **the default state of every fresh deployment** — so
Pass 2 recorded nothing and the persisted row read as though it never executed. Stage creation was a
coincidence of which code paths happened to touch a stage-creating mutator. Fixed by an eager
`begin_stage()` on capture entry, so "this pass ran" is observable **by construction**.

The lesson generalises past this bug: when a diagnostic's output can omit a step it actually
performed, the omission reads as a fact about the system rather than a gap in the instrument.

Fourth instance, and it was committed in **this document**: the `ROUTING_TRACE_APP_MCP_MODE` row was
written struck-through and past-tense ("Removed") while the setting was still live in `config.py`,
its removal merely queued. Two independent agents misread it as a completed change within an hour —
the same evidence standard we accepted for `RETENTION_DAYS = 0`. **A decision recorded in a plan is
not a change made in the tree, and when the two can be confused the plan must say which it is.**
Rule 1 governs the source of truth as much as the code it describes.

**A guard whose correctness depends on the test harness is not a guard.** `logging` interpolates
lazily and *swallows its own formatting errors* in production; pytest's `LogCaptureHandler` overrides
`handleError` to **re-raise**. So `logger.warning(..., exc)` with a poison `__str__` destroys the
exception under test and would not in production. The mirror image of the orchestration note below:
there the harness made dead production code look alive, here it makes a production-safe expression
look dangerous — and it would equally hide the reverse, which is the direction that ships bugs.
**The test environment is not a neutral observer of guard behaviour; establish which one you are
measuring before believing either result.** Pre-format rather than relying on lazy interpolation.

Related, and the reason contracts get audited too: `clamp()` documented itself as *"Total by
design"* while its first statement, `if not text:`, sat outside its own `try` — a raising `__bool__`
escaped. Every call site happened to be guarded, so nothing broke; the hazard was that **the next
instrumentation point would trust the docstring.** Rule 1 applied to a contract: a thing that is not
safe, labelled safe.

**A partitioned test run cannot detect a cross-partition defect.** Gate evidence must come from
**one combined run** across all relevant scopes; per-scope runs are fine while iterating and are
never evidence of closure. This was designed in deliberately — per-scope runs give better diagnostic
resolution when something breaks — and the cost was invisible because the partition did not merely
reduce sensitivity to session-state-dependent defects, it **eliminated** it. Three "all green"
reports were assembled from runs structurally incapable of seeing a defect that had already bitten
twice. Run the combined invocation **twice**: order-dependent session state is what this class is
made of, and one ordering proves one ordering.

**An orchestration note, learned the same way.** Agents that share no *files* can still share a
*build*. Scheduling a test-writing agent in parallel with a multi-file implementation agent on the
grounds that their file ownership was disjoint produced a collection-time `ImportError` from a
half-applied edit — the writer's job is to *execute* the tree the developer is *mutating*. Textual
coupling is not the only kind: **anything that executes the tree must serialise behind anything that
mutates it.** Corollary for reading results: a test outcome captured while another agent is mid-edit
is not evidence of anything, in either direction.

**How this class gets found — and it is not by code review.** Three of the four Rule 1 instances
above were surfaced by an agent *checking whether a claim was true*, not by anyone reading code
looking for bugs: the retention sentinel, the omitted Pass-2 stage, and this document's own row. The
`stages[].prompt` template-length trap is a fifth of the same shape. Reading code confirms it does
what it says; only verification establishes whether what it says is true. Budget for the second
activity explicitly — a review pass will not substitute for it.

**And verification effort is misallocated in a predictable direction:** claims you reason *from* get
checked; claims you reason *around* do not. Four incidental claims went unverified in this run — a
model's fields, a library's attribution, an exposure count, the existence of a README section — each
one command away, each skipped because it was framing rather than substance. Framing is exactly
where an unchecked claim survives longest, because nothing downstream depends on it hard enough to
fail. A standing instruction to push back is a good default and a poor primary defence: if it is the
only thing catching these, it is load-bearing, and it will miss the one nobody questions.

**Instrument errors outlive fact errors.** A plain wrong claim is caught by re-measuring. An
*instrument* error survives that, because the measurement is real — it just answers a different
question than the one asked. Two this run: `tests/architecture/ → 2` was a true count of one file
reported under the directory's name, and "zero schemas regenerated" came from grepping
`export const $Name` when this project's generator emits `export const NameSchema`. Both numbers
were honest. Both meant something other than what they were used for. **Re-running the command
reproduces the error; only inspecting what the instrument actually matched exposes it.** When a
measurement is load-bearing, check the pattern, not just the result. A third instance is not a
measurement at all but a *command*: `git checkout -- <dir>/` on a path mixing tracked and untracked
files exits 0 while **silently skipping the untracked ones**. The exit code is honest about the half
it did; the caller reads it as covering both. **A command that partially succeeds belongs to this
family** — verify the effect, not the status.

Fourth instance, and it is in a *technique this repo recommends*: `CLAUDE.md` suggests scoping a
frontend typecheck with `npx tsc --noEmit | grep -E "ComponentA|ComponentB"`. **`tsc` keys its
output on file paths, so a symbol-shaped filter silently misses every file whose name lacks the
domain word.** Measured, not assumed: a deliberate `TS2322` injected into 11 new components was
caught in 2 of them by a five-alternative symbol pattern. A clean reading would have been an honest
answer to a different question. **Filter by directory** (`grep "Admin/ServerChannels"`), and treat a
scoped run as trustworthy only against a separately-verified unfiltered one.

Fifth instance, and the worst-sited — it was in the *verification command adopted to guard against
half-landed state*. `find` on this machine is **`bfs`, not GNU findutils**, and it rejects relative
timestamps: `-newermt '-60 minutes'` prints an "Invalid timestamp" error and **exits 1**. The tool
fails loudly. The failure became silent because the caller wrote
`find ... -newermt '-30 minutes' 2>/dev/null | head` — **`2>/dev/null` discarded the message and the
pipe replaced the exit code with `head`'s zero.** Empty stdout then read as "nothing changed".

**The lesson is not that verification can fail silently; it is that a loud failure was silenced by
the invocation.** Suppressing stderr and piping are the two habits that convert a diagnosable error
into a false all-clear, and they are exactly the habits a one-liner encourages. Do not discard stderr
on a command whose *absence of output* is the finding. Prefer `ls -lt` and read mtimes, or
`-newer <reference-file>`, which `bfs` does support. And validate any verification command against a
case it must catch — the same discipline demanded of tests.

That entry was itself wrong when first written here: it claimed a bare untracked path silently
no-ops, which was reasoned, relayed, codified into `backend/tests/README.md` rule 8, and only then
executed — at which point it turned out to error loudly (exit 1). Four steps, no execution. **A rule
about not trusting exit codes propagated on an untested claim about an exit code.** The conclusion
survived by luck; `cp`-first is correct under either mechanism.

**A refactor that unifies N call sites falsifies documentation for N features, not one.** Phase 5
collapsed three `_ai_classify` copies into `AgentClassifier` and broke `route_to_agent()` call-path
claims in six files across Server Channels, App MCP Server, Identity MCP Server and Prompt Examples.
The doc work was scoped as "update this feature's docs", which was the wrong unit: **the docs of the
thing being changed are the obvious target; the docs of everything that *called* it rot silently,
because nobody owning them knows the change happened.** Same asymmetry as the `security_events.py`
note — the dependency is invisible from the side that breaks it. When a change removes or relocates a
public call path, enumerate its callers' *features*, not just its callers' code. This project's own
`docs/README.md` registry is the enumeration.

**A test is only as falsifiable as the assumptions it leans on.** The Phase 3 audit-row test proves
*which* admin was recorded only indirectly — nothing reads `user_id`; it relies on
`GET /security-events/` being self-scoped, so a wrongly-attributed event vanishes from the querying
admin's feed. Falsifiable today, and it fails silently the day that endpoint is widened to
"superuser sees all". Nothing would flag it. Same family as every inventory here: **a guarantee
resting on a condition nobody wrote down.** Note it where the condition lives — the endpoint — not
only where the guarantee is claimed.

Corollary, from the S5 ruling: a control must not appear to do *more* than it does either.
`ROUTING_TRACE_STORE_MESSAGE_TEXT=False` hides existing text but does not erase it — so it says so,
and names what does erase.

### Rule 2 — the debugging aid must never break the thing it observes

This feature exists to diagnose routing. Every time it has failed, it has failed by *causing* the
outage it was meant to explain. Four instances, all the same shape — an unguarded expression
evaluated *before* the guarded callee is entered:

1. `clamp()` as a bare argument expression in three `record_*` helpers — an object with a raising
   `__str__` propagates into the routing hot path.
2. `clamp()` + `_sha256()` unguarded in `RoutingTrace.__init__` — worse, because it runs inside
   `capture().__enter__` and aborts the whole routing pass rather than dropping a field.
3. `persist()` borrowing the caller's session — a failed diagnostic write silently discarded the
   caller's uncommitted work, and an expired-instance reload sent `REPLY_SETUP_FAILED` to a sender
   whose message had routed fine.
4. The `persist()` signature change landing without its call sites — `TypeError` raised at the call
   site, *before* `persist`'s never-raises guard could apply, breaking channel routing outright.

**Mutation checks have one honest exception, and it must be named rather than taken.** A
*regression guard* asserts behaviour the code could plausibly break, and is worthless until shown to
fail — demand the mutation. A *precondition assertion* pins a fact the surrounding design fixes
(e.g. "a standalone agent has no auto-route"), and breaking it on purpose means changing a rule the
phase puts out of scope; the mutation would be theatre. State which kind an assertion is when you
skip the check. A universal demand with no stated exception invites either quiet skipping or
meaningless mutations, and both look identical to a reader.

**The test for a new instrumentation point is not "is the recorder guarded" but "can anything in
the caller's argument list raise".** Prove it by firing a poison object (raising `__str__`,
`__bool__`, `__eq__`, `__hash__`, `__getattr__`, **`__len__`**), not by reading the code. `__len__`
on a `str` subclass escaped two separate helpers and was not on this list until it did.

**Fixing an instance in a location does not fix the location.** Instance 2 above guarded `clamp()`
and `_sha256()` in `RoutingTrace.__init__`. Three lines higher in the same constructor,
`_str_or_none` coerced `user_id` / `channel_id` / `actor_user_id` with a bare `str()` — directly
above the guarded block whose comment explains why coercion there must not raise. It survived the
fix made for it, and it was **live, not latent**: a poisoned `__str__` aborted `capture().__enter__`
and took the routing pass with it. Whoever fixed instance 2 verified their fix and stopped. When a
defect is found in a function, audit the function — and put the guard in the shared helper, not in a
`try` around today's call sites, so a field added later inherits it instead of depending on the next
author noticing a comment.

---

## 12. Risks

- **Contextvar-across-thread propagation** is correct but subtle. Without the explicit unit test, a
  future refactor breaks capture silently — and silent loss of a debugging aid is the worst failure
  mode for a debugging aid.
- **Recording must never fail delivery.** Swallow inside the recorder; keep call-site argument
  expressions trivial (see §5).
- **Simulate costs real LLM spend** and exposes another user's installed-agent list to the admin.
  Rate-limited and audited — but confirm the exposure is acceptable before shipping Phase 3.
- **Phase 5 changes routing outcomes.** Do not merge it with Phase 4.
- **Write volume** is bounded today (new threads only, behind the 120/min webhook rate limit)
  because `origin="server_channel"` is the only capture that exists. Adding `origin="app_mcp"`
  removes that bound — App MCP routes every message — so that change must reintroduce a
  metadata-only default alongside it (§4).
