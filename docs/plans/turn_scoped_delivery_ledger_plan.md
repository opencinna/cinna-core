# Turn-Scoped Outbound Delivery + Channel Turn-Delivery Ledger — Implementation Plan

**Status: PLANNED — not implemented.**

## 1. Feature summary

Direct follow-up to the Google Chat streaming feature (commit `638f065d`, plan
`docs/plans/google_chat_streaming_updates_plan.md` — read it for background, but where the two
disagree, THIS plan and the deviations recorded in
`docs/application/server_channels/server_channels_tech.md` win).

Two layered mechanisms:

**Layer A — turn identity in the stream events (bug fix, no migration).**
`STREAM_COMPLETED` / `STREAM_ERROR` / `STREAM_INTERRUPTED` event meta gains
`agent_message_id: str | None` — the id of the agent `SessionMessage` row this batch
created/finalized, or an explicit `None` meaning "this turn created no agent message". Outbound
consumers deliver **exactly that message**, never "the newest agent message in the session".

This fixes a real, currently-shipping bug: `ChannelOutboundService.handle_stream_completed`
resolves its text via `_last_agent_message` (`channel_outbound_service.py`, ~line 656 — newest
`role="agent"` row by `sequence_number`), which is not scoped to the completing turn. A turn that
produces no assistant text (tool-only turn, empty model output) re-delivers the **previous
turn's answer** into the thread as if it answered the new question. The email integration's
sending service subscribes to the same events and must be checked for (and cured of) the same
pattern in the same pass — a fix at the emitter should serve every subscriber deliberately.

**Layer B — a durable per-turn delivery ledger (one migration).** New table
`channel_turn_delivery` mapping a turn's agent message to the external messages that carry it.
Boundary-only writes (seal / final / notice adoption — NEVER the rolling 3-second draft
patches). It makes completion idempotent against duplicate events, makes crash recovery
knowledgeable about already-posted sealed messages, turns the relay-vs-finalized-text divergence
policy into an observable check instead of a silent assumption, and generalizes to any future
transport (Slack/Discord/Telegram: draft→sealed→final; no-edit transports: sealed parts only;
email: one `final` row).

Decided with the user (do NOT re-litigate):
- **Mismatch policy**: at completion, if the finalized canonical text no longer starts with the
  delivered prefix (length + sha256 check), deliver the relay tail as today, mark the row
  `diverged`, log. Revisit only if logs show it firing.
- Rolling draft patches are ephemeral by design — no per-flush persistence, ever.
- The relay stays authoritative for tail *content* mid-turn; the ledger records outcomes at
  boundaries and is the source of truth for "what is already standing in the thread".
- `ChannelThreadBinding.status_message_id` survives as a fast cache of the current draft;
  dropping it is out of scope.
- Phase C ideas (finalize-time re-patching of sealed messages to canonical text, message-edit
  propagation, email `visible_char_end` adoption) are OUT of scope — do not build toward them
  beyond what the ledger schema already provides.

## 2. Read first

- This plan, then `docs/README.md` → Server Channels → `server_channels.md` +
  `server_channels_tech.md` (the tech doc's deviation notes from the streaming feature are
  binding contracts).
- `backend/app/services/server_channels/channel_outbound_service.py` — whole file. Its
  never-raise / expired-instance discipline (total helpers, §11a rules in docstrings) applies to
  every new line here.
- `backend/app/services/server_channels/channel_stream_relay.py` — the relay whose seal/final
  boundaries become ledger writes. Its contracts are load-bearing (see §5).
- `backend/app/services/sessions/message_service.py` — emission sites:
  - LLM-batch `STREAM_COMPLETED` emission (~line 2898, guarded `if not was_interrupted:`),
    with `_finalize_agent_message` and `agent_message_id` in scope ~30 lines above;
  - LLM-batch `STREAM_INTERRUPTED` (~line 2584); LLM-batch `STREAM_ERROR` (~line 2920);
  - **command-stream** terminal events (~lines 2404–2425: `STREAM_INTERRUPTED` /
    `STREAM_ERROR` / `STREAM_COMPLETED` for `stream_command_via_agent_env`).
- `app/main.py` (~lines 270–365) — subscriber registrations; find the email sending service's
  stream-event subscribers and read that service.
- `backend/app/models/server_channels/` + `backend/app/alembic/versions/` conventions;
  migrations run via Docker (`make migration` / `make migrate`), models re-exported in
  `models/__init__.py`.
- `backend/tests/README.md`; `tests/api/server_channels/` conventions
  (`server_channels_streaming_updates_test.py`, `server_channels_status_notice_test.py` — the
  `_Chat` adapter mock, `StubAgentEnvConnector`, `drain_tasks`, settings overrides).

## 3. Facts the design depends on (verified during the previous feature — re-verify cheaply, don't re-derive)

- One agent `SessionMessage` row is created per **LLM batch** (on first assistant event), and
  `STREAM_COMPLETED` fires once per LLM batch — so "deliver by the event's
  `agent_message_id`" is naturally per-batch-correct for multi-batch turns.
- A batch with no assistant events creates **no** agent message row (finalize is guarded by
  `if streaming_events:`), which is exactly when the stale read bites today.
- `STREAM_COMPLETED` is NOT emitted for interrupted LLM streams; `STREAM_INTERRUPTED` is.
- The relay (`channel_stream_relay.py`) tracks delivered content in visible space
  (`_visible()` — tag-strip regexes imported from `message_service`, drift-guard test exists),
  and stripping is **additive across seal cuts** (proved during review: cuts never split a tag),
  which is what makes `visible_char_end` a stable thing to persist.
- `take_tail()` is tristate: `None` = relay failed → full-text fallback; `("", False)` =
  genuinely empty. Bus handlers never call `relay.stop()` (per-batch events; only
  `on_complete`/`on_error` stop it). Preserve both contracts.
- `StubAgentEnvConnector` yields canned events with no await, so the relay flusher may never run
  in API tests — rolling-draft assertions can pass **vacuously**. Any new test asserting
  intermediate behavior must prove non-vacuity (the existing tests show the pattern; several
  have explicit mutation proofs).

## 4. Phases

### Phase A — `agent_message_id` in stream-event meta + turn-scoped consumers (no migration)

1. **Emitters** (`message_service.py`): add `agent_message_id` (stringified UUID or `None`) to
   the meta of the LLM-batch `STREAM_COMPLETED`, `STREAM_ERROR`, and `STREAM_INTERRUPTED`
   emissions. For `STREAM_ERROR` raised before any assistant event, `None` is correct and
   expected.
2. **Command streams** (investigation + explicit decision, recorded in the tech doc): the
   command-stream terminal events (~2404–2425) relate to command output messages, not agent
   messages. Establish what a channel-originated command turn actually delivers outbound
   **today** (trace `handle_stream_completed` → `_last_agent_message` for a command-stream
   completion — this is another instance of the stale-turn bug: no agent row is written, so
   today it re-delivers the previous answer). Ship: `agent_message_id=None` on command-stream
   events + the consumers' `None` branch (below), and record the resulting channel-visible
   behavior for command turns explicitly. Do not invent command-output delivery in this plan.
3. **Channel consumer** (`channel_outbound_service.py`): all three handlers read
   `meta["agent_message_id"]`.
   - Present → load THAT message; its content (empty ⇒ treat as absent) replaces every
     `_last_agent_message` call on these paths.
   - `None` (or the message row is gone) → the turn said nothing: existing
     `clear_binding_status` / settle behavior. Never fall back to newest-row.
   - **Backward compatibility**: meta *missing the key entirely* (an event from code predating
     this change, e.g. during a rolling deploy or a stale test fixture) → keep today's
     `_last_agent_message` behavior, log at debug. Key present is authoritative.
   - Relay-present paths are unchanged (tail is already turn-scoped); this fixes the
     relay-absent / relay-failed arms and the interrupted/error fallbacks.
4. **Email consumer**: find the email sending service's `STREAM_COMPLETED`/`STREAM_ERROR`
   subscribers (registered in `main.py`), check for the same newest-row pattern, and apply the
   identical contract (deliver by id; `None` ⇒ do not send; missing key ⇒ legacy behavior).
5. Remove `_last_agent_message` if nothing legitimate still calls it; otherwise leave it with a
   docstring stating it must never be used for turn attribution.

### Phase B — `channel_turn_delivery` ledger (one migration)

1. **Model** `backend/app/models/server_channels/channel_turn_delivery.py` (+ re-export):
   - `id` UUID PK; `binding_id` FK → `channel_thread_binding.id` CASCADE;
     `session_message_id` FK → `session_message.id` CASCADE (the agent row);
     `part_index` int; `role` varchar (`draft` | `sealed` | `final` | `notice`);
     `external_message_id` varchar(255) — opaque, transport-owned;
     `visible_char_end` int nullable; `content_sha256` varchar nullable (hash of the delivered
     visible prefix); `status` varchar (`delivered` | `failed` | `diverged`);
     `created_at` / `updated_at`.
   - Unique constraint `(session_message_id, part_index)`.
   - Alembic migration via Docker; review the autogenerate before applying.
2. **Writers** (boundary-only; every write total/never-raising, commit patterns copied from
   `_persist_status_message_id`):
   - Relay **seal** → insert `role=sealed` row (next `part_index`, the sealed slice's
     `visible_char_end` and prefix hash, the settled message's external id).
   - Relay fresh-draft creation after a seal → `role=draft` row (superseding: on seal, the
     draft row it settled becomes the `sealed` row — update in place rather than
     insert-and-orphan).
   - Final tail delivery in the three handlers → the current draft row (or a new row when none)
     becomes `role=final`.
   - Notice adoption (`adopt_status_notice`) → `role=notice` row, `session_message_id` NULL is
     NOT allowed by the FK — so notice rows attach only once an agent message exists; before
     that, `binding.status_message_id` alone carries the notice exactly as today. (If this
     makes `notice` rows nearly useless in practice, dropping the `notice` role and keeping
     drafts/seals/finals only is an acceptable reviewer-approved simplification — record it.)
   - A failed boundary delivery → `status=failed` on the row; never blocks the turn.
3. **Readers**:
   - `handle_stream_completed`: before delivering, check for an existing `role=final` row for
     this `agent_message_id` → no-op (idempotency against duplicate/racing events and
     scheduler flushes).
   - Completion prefix check (the decided mismatch policy): compare the finalized message's
     visible text against the max `visible_char_end` + `content_sha256` of its sealed rows.
     Match → deliver remainder; mismatch → deliver the relay tail as today, mark `diverged`,
     log a warning.
   - `handle_stream_interrupted` / `handle_stream_error`: "was anything delivered this turn?"
     becomes a ledger read when the relay is absent (today it's unanswerable in those arms).
4. **`binding.status_message_id`** stays and stays authoritative for "which message do I patch
   next"; the ledger's `draft` row mirrors it. A divergence between the two is resolved in
   favor of the binding column, logged.
5. **Debug panel** (cheap, optional if time allows — manager's call): expose per-turn delivery
   rows in the channel debug feed detail rather than building UI.

### Phase C — tests

- Unit: emitter meta correctness (all three event types, LLM + command streams, the
  no-assistant-text batch → `None`); consumer branch matrix (present / `None` / missing key /
  row deleted); ledger writer state machine (draft→sealed→final transitions, unique
  `part_index`, failed write never raises); prefix-check match and mismatch paths.
- API (`tests/api/server_channels/`):
  - **The bug's reproducer, end-to-end**: turn 1 delivers answer A; turn 2's stream yields
    only tool events (no assistant text) → the thread must NOT receive answer A again; the
    notice is cleared/settled. This is the headline test — it fails on today's code.
  - Same shape through the interrupted path (early `/stop`, zero assistant events) — must keep
    the visible "⏹️ Stopped." acknowledgement (regression guard on the previous feature's fix).
  - Duplicate `STREAM_COMPLETED` for the same agent message → exactly one delivery
    (ledger idempotency).
  - Divergence path: sealed prefix + a finalized text that no longer matches → relay tail
    delivered, row marked `diverged`.
  - Email: the analogous stale-turn test against the email sending service if the pattern was
    confirmed there (Phase A.4).
- Regression scope: `tests/unit/ tests/api/server_channels/` plus the shared streaming scopes
  (`tests/api/agents/sessions/ a2a_integration/ app_mcp/ routing/`) — same scope the previous
  feature used, since `message_service.py` emission changes touch every streaming consumer.
  Full suite at the end.

### Phase D — docs

- `server_channels_tech.md`: event-meta contract (`agent_message_id`, the `None` and
  missing-key semantics), ledger schema + boundary-write rule + mismatch policy, command-turn
  decision from Phase A.2.
- `server_channels.md`: short business-level note (a turn's reply is attributed by turn
  identity, never by recency; what a reader sees on an empty turn).
- `docs/README.md` only if a row's description is now wrong. No new rows, no counter columns.

## 5. Invariants that must survive review

1. No consumer ever again derives turn attribution from "newest row" — meta-key-present is
   authoritative; missing-key legacy fallback is the only place the old query survives.
2. Ledger writes are boundary-only and total (never raise, never block a delivery); a lost
   ledger write costs observability, never a reply.
3. Relay contracts unchanged: `take_tail()` tristate, bus handlers never `relay.stop()`,
   visible-space floor, `set_binding_status` bool return consumed.
4. `channel_outbound_service` discipline: every new ORM attribute read on these paths goes
   through a total helper or equivalent guard (§11a — read the file's docstrings first).
5. Event-meta change is additive; no existing subscriber breaks when the key is absent.
6. Email transport behavior changes ONLY where the stale-turn bug is confirmed there;
   everything else identical.
7. No OpenAPI client regeneration (nothing request/response-shaped changes). Exactly one
   migration, for the ledger table.

## 6. Out of scope

- Finalize-time re-patching of sealed messages to canonical text; message-edit propagation.
- Dropping `binding.status_message_id`.
- Command-output outbound delivery for channel command turns (decide + document only).
- Multi-worker coordination (single-process assumption unchanged).
- Frontend changes (none).
