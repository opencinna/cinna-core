# A2A crash recovery (C1–C4): implementation plan

Source spec (approved): `cinna-desktop/drafts/crash_recovery/cinna_core_reply.md` §1–§5. Out of scope: C5 (`contextId` fallback, `taskId` in error data) and the OpenCode session-delete follow-up.
Backend-only. No migration, no route or schema change, **no client regeneration**. Paths are relative to `backend/app/` unless they start with `backend/`, `docs/` or `frontend/`.

## §0 Index

**Working-tree rules.** Base: `main` @ 06d05c58, clean. Phases land in order and each one leaves the suite green. Do not touch `process_pending_messages` (`services/sessions/message_service.py:1501-1668`) except where §3 says so. No git write operations by phase agents.

**Decisions** (★ = the chosen option, alternatives in one line each)

| # | Decision |
|---|---|
| D1 | ★ The A2A producer runs detached, in a module-level strong-reference task set. It is **not** started via `create_task_with_error_logging`, which keeps no reference and which tests patch into a collector (`tests/utils/fixtures.py:286`). |
| D2 | ★ The turn lock plus teardown is a generic `LockedTurnRunner` in `stream_processor.py`, used by A2A only. The web path keeps its hand-written block (its comment at `message_service.py:1534-1562` stays true). Rejected: moving the web path onto the runner now (regression risk for no gain). |
| D3 | ★ "Turn in flight" means: session lock held **or** a stream registered in this process **or** `interaction_status` ∈ {running, pending_stream} **or** undelivered user messages exist. This relies on process-local state, the same as the existing session lock and live buffer (`docker-compose.yml:9` runs one worker). |
| D4 | ★ The new message status value is `"aborted"`, written to the existing `message.status` column. The spec's `canceled` maps to the existing `"user_interrupted"`. |
| D5 | **Superseded by D10.** ~~Orphan detection uses a process **boot stamp** (`stream_owner = {boot_id, boot_at}`), written into agent-message metadata and `session_metadata`. Rejected: an age-only rule (a crashed turn would report `working` for up to 2 h); a startup sweep (unsafe with several workers).~~ |
| D6 | ★ Minimum age for orphan repair: **2 min** (`STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES`), measured on the heartbeat (D10): 4 missed beats. Rows without a heartbeat (legacy, written before D10) fall back to `STATUS_REPAIR_STREAM_MAX_AGE_MINUTES` (120). See §3.4. |
| D7 | ★ The client `messageId` is stored in `message_metadata["client_message_id"]` and looked up per session. The lookup is indexed through the leading `session_id` of `UniqueConstraint("session_id","sequence_number")` (`models/sessions/session.py:100`), so no migration is needed. Rejected: a partial unique expression index (needs a migration on a `JSON` column, and blank keys collide unless normalised to NULL). |
| D8 | ★ Dedupe runs inside `SessionService.send_session_message`, before any command runs or any row is written, so it covers both `message/send` (via `ChannelIngestionService`) and `message/stream`. A process-local in-flight claim set closes the race between check and insert. |
| D10 | ★ Orphan detection uses a **DB heartbeat** (phase P3b). A per-turn task (`services/sessions/stream_heartbeat.py`, started in `stream_message_with_events`, stopped before the final write and before the seal) writes `stream_heartbeat_at` every 30 s into `session_metadata` from turn start and into the in-progress agent row once it exists; the row creation and every flush stamp it too. Each beat locks the row, re-reads it, sets only its key; a stop flag checked under the lock keeps a late beat from landing after the seal. The orphan pass acts only on a heartbeat older than 2 min; in-process state (stream registry, session lock) is a skip guard only. Pass B also skips a `running` session with a fresh heartbeat, up to `STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS` (12, R13). The orphan pass re-checks claim and heartbeat and clears under one row lock. `stop()` removes the session stamp at turn end (R14). Self-heal: a beat reopens an `aborted` row, and restores `running` on a session the orphan pass cleared for this turn (marker `stream_orphan_cleared_heartbeat`). The ticker is off under `settings.TESTING` (the API harness shares one DB session across threads). Rejected: in-process memory / boot stamp (D5), not valid across workers: a respawned worker judged its siblings' live turns dead, and the 120-min fallback aborted live turns longer than 2 h. |
| D9 | ★ A duplicate whose stored message is still `pending` and has no turn in flight **re-drives delivery** without storing a new row. That is not a second run, because the message was never sent. |

**Phases** (owner of every phase: backend developer)

| Phase | Scope | Files touched | Plan § | Tests (§6) |
|---|---|---|---|---|
| P1 | C1: detach the producer, wait-mode lock, shielded teardown, final status when nothing is pending | `services/sessions/stream_event_handlers.py`, `services/sessions/stream_processor.py`, `services/a2a/a2a_request_handler.py` | §1, §5 | T1 |
| P2 | C2: `tasks/get` state, cheap state probe, `message/send` poll | `services/a2a/a2a_event_mapper.py`, `services/a2a/a2a_task_store.py`, `services/a2a/a2a_request_handler.py`, `services/sessions/active_streaming_manager.py`, `services/sessions/stream_processor.py`, `services/sessions/message_service.py` (query helpers only) | §2 | T2 |
| P3 | C3: finalize on cancel, boot stamp, orphan repair pass, `cinna.message_state` and live-buffer merge in history | `services/sessions/stream_owner.py` (new), `services/sessions/message_service.py`, `services/sessions/session_service.py`, `services/system/status_repair_sessions.py`, `services/system/status_repair_scheduler.py`, `core/config.py`, `services/a2a/a2a_event_mapper.py`, `services/a2a/a2a_task_store.py`, `services/a2a/a2a_request_handler.py`, `models/sessions/session.py` (comment only) | §3, §5 | T3 |
| P4 | C4: store, echo and dedupe the client `messageId` | `services/sessions/session_service.py`, `services/sessions/channel_ingestion_service.py`, `services/a2a/a2a_request_handler.py`, `services/a2a/a2a_task_store.py`, `services/a2a/a2a_event_mapper.py` | §4 | T4 |
| P3b | D10: replace the boot stamp with a DB heartbeat; orphan pass and Pass B act on it | `services/sessions/stream_heartbeat.py` (new; `stream_owner.py` deleted), `services/sessions/message_service.py`, `services/sessions/session_service.py`, `services/system/status_repair_sessions.py`, `services/system/status_repair_scheduler.py` (comment), `core/config.py` (comment) | §3.1, §3.4, §9 | T3 (heartbeat variants), `tests/unit/test_stream_heartbeat.py` |

Docs: §7. Regression groups: §8. Risks: §9.

**Both surfaces share one code path (verified).** `/api/v1/a2a` builds `A2ARequestHandler` (`api/routes/a2a.py:275`). `/external/a2a` dispatches to `ExternalA2AContextHandler(A2ARequestHandler)` through `ExternalA2ARequestHandler._dispatch_context` (`services/external/external_a2a_request_handler.py:436,444,453`). The subclass overrides only scope hooks and `handle_tasks_cancel` (`services/external/external_a2a_context_handler.py:106-259`). It does **not** override `handle_message_stream`, `handle_message_send` or `handle_tasks_get`, so every change below lands on both surfaces. Tests cover both (T1.4, T2.5, T4.5). Keep it that way: no phase may add an override of those three methods.

---

## §1 P1: detach the producer (C1)

### 1.1 `services/sessions/stream_event_handlers.py`, `A2AStreamEventHandler` (`:194-354`)
- Add a module-level `_DETACHED_A2A_TURNS: set[asyncio.Task] = set()` and `async def wait_for_detached_a2a_turns(timeout: float) -> None`, which gathers a snapshot of the set. Tests use it; shutdown code may use it later.
- Constructor gains `on_turn_released: Callable[[], Awaitable[None]] | None = None` (not needed if the runner in §1.2 calls back directly; choose one, not both). Add state: `self._consumer_attached = True`, `self.final_emitted = False`.
- New `_emit(item)`: return if `not self._consumer_attached`; otherwise `self.queue.put_nowait(item)`. The queue stays unbounded (`maxsize=0`), so `put_nowait` never raises and **never blocks**. `on_event` and `_enqueue_error_once` use `_emit` instead of `await queue.put`. The done-callback sentinel also goes through `_emit`.
- `on_event`: after mapping, set `final_emitted = True` when the mapped event is final. Read the flag from the mapper's return shape at `a2a_event_mapper.py:150-215`. `_enqueue_error_once` also sets `final_emitted`.
- New `async def emit_final_state(state: TaskState)`: no-op if `final_emitted`; otherwise `_emit(create_status_update(state=..., final=state is terminal))` and set the flag.
- `stream()` (`:282-307`): add the producer to `_DETACHED_A2A_TURNS` and discard it in its done-callback. The `finally` **no longer cancels** the producer. It sets `_consumer_attached = False` and drains the queue with `get_nowait` until empty. Update the class and method docstrings (the "Client disconnect → cancels the producer" bullet is now false).
- `_run_processor`: unchanged semantics. `CancelledError` now comes only from shutdown and still re-raises.

**Invariants.** After a disconnect, nothing references the consumer, the queue is empty and receives no more items, and the producer runs to completion. With an attached consumer the order of events is unchanged.

### 1.2 `services/sessions/stream_processor.py`
- Add `class LockedTurnRunner` with `__init__(self, *, processor, session_id: UUID, teardown_reason: str, after_teardown: Callable[[], Awaitable[None]] | None)`. `async def process()` does:
  ```
  async with get_session_lock(str(session_id)):   # WAIT mode, not SessionLockBusyError
      try:
          return await processor.process()        # processor built with use_session_lock=False
      finally:
          try: await asyncio.shield(SessionService.clear_interaction_status(session_id, reason=teardown_reason))
          except Exception: log warning
          if after_teardown: await after_teardown()   # still inside the lock
  ```
  The docstring points to the reasoning at `message_service.py:1534-1562` rather than copying it. The whole body, including teardown, runs inside the lock, and the shield residual is the same one documented there.
- Add `def is_session_lock_held(session_id: str) -> bool`. It returns `_session_locks.get(session_id)` and checks `.locked()`, **without creating an entry** (used by P2).

### 1.3 `services/a2a/a2a_request_handler.py`, `handle_message_stream` (`:604-627`)
- Build the `SessionStreamProcessor` as today (`use_session_lock=False`).
- Wrap it: `runner = LockedTurnRunner(processor=processor, session_id=session_id, teardown_reason="a2a stream teardown", after_teardown=_final_if_silent)`. `_final_if_silent` runs `await handler.emit_final_state(<state>)`.
  - After P2, `<state>` is `self.task_store.get_state(str(session_id))`, mapped so that non-terminal becomes `completed`: the lock is still held and the teardown has run, so no turn of this session is in flight.
  - In P1, use `TaskState.completed`.
- `async for sse_event in handler.stream(runner): yield sse_event`.
- The initial `working` event (`:581-588`) is still yielded **before** the lock is taken, so a queued second message gets `working` immediately (spec Q6).

### 1.4 Checks
`docker compose exec backend python -m pytest tests/unit/test_a2a_stream_event_handler.py tests/api/a2a_integration/ tests/api/agents/sessions/agents_session_stream_concurrency_test.py -q`

---

## §2 P2: `tasks/get` state (C2)

### 2.1 Helpers
- `services/sessions/active_streaming_manager.py`: `def is_streaming_nowait(self, session_id) -> bool`, a plain dict membership check. It may only be called on the event-loop thread; say so in the docstring.
- `services/sessions/message_service.py` (next to `get_last_message`, `:930`): add `has_pending_user_messages(db, session_id) -> bool` (EXISTS on role=`user` and `sent_to_agent_status="pending"`) and `get_last_agent_message_of_current_turn(db, session_id)`, which returns the newest `agent` row whose `sequence_number` is greater than the newest `user` row's, or `None`.

### 2.2 `A2AEventMapper.map_session_status_to_task_state` (`a2a_event_mapper.py:469-502`)
New signature: `(status, interaction_status, tool_questions_status=None, *, turn_in_flight=False, has_pending=False, last_agent_status=None)`. Evaluate in this order and stop at the first match:
1. `tool_questions_status == "unanswered"` → `input_required` (unchanged)
2. `interaction_status == "running"` → `working` (unchanged)
3. `interaction_status == "pending_stream"` → `submitted` (unchanged)
4. `turn_in_flight or has_pending` → `working`
5. `status == "error"` → `failed`
6. `last_agent_status == "user_interrupted"` → `canceled`, and `== "aborted"` → `failed`
7. `status in ("active", "completed")` → `completed`
8. anything else → `working` (conservative; unknown statuses keep today's answer)

### 2.3 `services/a2a/a2a_task_store.py`
- `_map_status_to_state` (`:98-118`) computes:
  - `turn_in_flight = is_session_lock_held(str(id)) or active_streaming_manager.is_streaming_nowait(id)`
  - `has_pending = MessageService.has_pending_user_messages(...)`
  - `last_agent_status` from the §2.1 helper

  `tool_questions_status` keeps coming from `get_last_message` (unchanged).
- Add `get_state(task_id) -> TaskState | None`, which reads no history.

### 2.4 `handle_message_send` poll (`a2a_request_handler.py:421-437`)
Poll `self.task_store.get_state(...)` each second. Once the state is terminal or `input_required`, return `self.task_store.get(...)`, the same response shape as today. The first probe runs before the spawned `process_pending_messages` task has started; the pending row makes it `working` (rule 4), so the poll cannot return early.

### 2.5 Checks
`pytest tests/unit/ -q -k "a2a"`, `tests/api/a2a_integration/`, `tests/api/external/external_a2a_agent_test.py`

---

## §3 P3: finalize aborted turns (C3)

### 3.1 Stream heartbeat: `services/sessions/stream_heartbeat.py` (new, P3b; replaced the boot stamp of D5)
- `StreamHeartbeat(session_id, get_fresh_db_session)`: `start()`, `attach_message(id)`, `stop()`. Interval `HEARTBEAT_INTERVAL_SECONDS = 30`.
- Key `stream_heartbeat_at` (UTC ISO) in `session_metadata` and in the agent row's `message_metadata`. Beats lock the row and set only their key; a finalized row is skipped, an `aborted` one is reopened.
- `stream_owner.py` and every `stream_owner` write were removed.

### 3.2 `services/sessions/message_service.py`
- Add `_apply_aborted(db, msg, events: list | None, content: str | None) -> bool`.
  - Lock the row with `db.get(SessionMessage, id, with_for_update=True)`. Return `False` unless `message_metadata.get("streaming_in_progress")` is true, so a finalized row is never overwritten.
  - Set `status="aborted"` and `status_message="Turn aborted before completion"`.
  - Set `streaming_in_progress=False`. Keep `streaming_events` (the passed list if one was given, else the stored one). Set content to the joined assistant text if any, else keep it. Call `flag_modified` and commit.
  - Wrap it as `finalize_aborted_agent_message(message_id, events, get_fresh_db_session)`, which runs through `to_thread`.
- In `stream_message_with_events` (`:2764-3199`):
  - The initial metadata (`:2951-2955`) gets `stream_heartbeat_at` (P3b; was the boot stamp).
  - Add `except asyncio.CancelledError:` **before** `except Exception` (`:3173`). If `agent_message_id` is set, run `await asyncio.shield(finalize_aborted_agent_message(agent_message_id, list(streaming_events), ...))` inside `try/except Exception: log`, then `raise`.
  - Emit no terminal event. Every locked caller's teardown already clears `interaction_status`, and Pass D seals channel drafts.
  - The `finally` at `:3196` is unchanged.
- `_flush_streaming_to_db` (`:2143-2180`): also write `stream_heartbeat_at`. If `agent_msg.status == "aborted"`, reset it to `""` and clear `status_message`: a live writer proves the orphan pass guessed wrong (see §9 R3).
- ~~`_emit_activity_event(STREAM_STARTED, ...)` passes `stream_owner`~~ (removed in P3b).
- `enrich_messages_with_streaming` (`:1781-1817`): extract the event-sequence merge into a pure function `merge_live_events(db_events, live_events) -> list` and call it from here. The rule stays the same: take live events whose `event_seq` is greater than the highest `event_seq` in the DB.

### 3.3 `services/sessions/session_service.py`, `handle_stream_started` (`:907-970`)
~~Copy `meta["stream_owner"]` into `session_metadata`.~~ Removed in P3b: the heartbeat writes the session side itself. A stale heartbeat does no harm because the pass also requires `running` with a non-null `streaming_started_at`.

### 3.4 Orphan pass: `services/system/status_repair_sessions.py`
- Add `async def repair_orphaned_streams(ctx: RepairContext) -> int` and register it in `status_repair_scheduler.py:116-139` as `("orphaned_streams", ...)`, **after `sessions` and before `input_tasks`**. Pass C re-derives task state from the session rows this pass clears. Update the tenants comment (`:94-98`).
- Setting: `STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES: int = Field(default=2, ge=1)` in `core/config.py` next to `:1163`.
- Messages (P3b, D10): select agent rows where `message_metadata->>'streaming_in_progress' = 'true'` and `timestamp` is older than the minimum age (bounded candidate query, G3). Per row, skip if this worker has the stream registered or holds the session lock. Then:
  - heartbeat present: repair when it is older than the minimum age; otherwise keep.
  - no heartbeat (legacy): repair only if age ≥ `STATUS_REPAIR_STREAM_MAX_AGE_MINUTES`, the session is not `running`, and the session is not in `ctx.sessions_in_motion`.
  - Repair locks the row, re-checks the heartbeat is unchanged, then `_apply_aborted(ctx.session, msg, None, None)`; rollback on error, per-row isolation as in `repair_sessions`.
- Sessions: `interaction_status == "running"`, `streaming_started_at` older than the minimum age, not in motion, not local, and `session_metadata.stream_heartbeat_at` older than the minimum age (no heartbeat: left to Pass B) → re-check the heartbeat, `_clear_to_idle` (compare-and-set on the claim), `ctx.note_session_in_motion`, then write the `stream_orphan_cleared_heartbeat` marker so a live turn can restore `running`. This covers a crash **before** the first assistant event, when no agent row exists.
- Pass B (B.1) skips a `running` session whose heartbeat is fresh, so a live turn longer than 120 min is not cleared; past `STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS` (12) it reaps regardless (R13).
- The session clear re-checks claim and heartbeat under `with_for_update` and commits the clear plus marker in that transaction (no call to `clear_interaction_status`, whose own connection would block on the lock); the WS event goes through `SessionService.emit_interaction_status_cleared`.
- **Why 2 minutes.** 4 missed beats at 30 s: a slow DB write or a busy loop never reaps a live turn, and a backend crash still reads as `failed` within about two ticks (4 min). The heartbeat is DB state, so the verdict holds on every worker.

### 3.5 History state and live buffer
- `A2AEventMapper.convert_session_messages_to_a2a(messages, session_id, live_stream: dict | None = None)` (`:505`). For each row with `role == "agent"`, set `Message.metadata["cinna.message_state"]`:
  - `aborted` when status is `aborted`
  - `canceled` when status is `user_interrupted`
  - `streaming` when `streaming_in_progress` is set
  - `complete` otherwise
- For the in-progress row, when `live_stream` is given, build parts from `merge_live_events(stored, live_stream["streaming_events"])` on **copies**. Never mutate the ORM row: the store's session would flush it.
- `_build_parts_for_session_message` takes an optional `events_override`.
- `get_task_with_limited_history(task_id, history_length, live_stream=None)` passes the buffer through. `handle_tasks_get` (`:651`) first runs `live = await active_streaming_manager.get_stream_events(session_uuid)`.
- Add a constant `MESSAGE_STATE_KEY = "cinna.message_state"` next to `CONTENT_KIND_KEY`.
- `models/sessions/session.py:114`: change the comment to `"" | "user_interrupted" | "error" | "aborted"`.

### 3.6 Checks
`tests/api/agents/sessions/`, `tests/api/a2a_integration/`, `tests/unit/`, `tests/api/acp_integration/`

---

## §4 P4: client `messageId` (C4)

### 4.1 Read and normalise
In `handle_message_send` and `handle_message_stream`, set `client_message_id = message_data.get("messageId") or message_data.get("message_id")`. Keep it only if it is a `str` that is still non-empty after `strip()` and at most 255 characters; otherwise treat it as `None` and log at debug level. Blank values never take part in dedupe (D7).

### 4.2 `SessionService.send_session_message` (`services/sessions/session_service.py:1381`)
- Add a keyword `client_message_id: str | None = None`. Pass it through `ChannelIngestionService.ingest_inbound_message` (`channel_ingestion_service.py:106-205`), which adds the same keyword. No other ingestion caller changes.
- When `client_message_id` and `session_id` are both set, and **before** the command/handler dispatch (`:1560`) and any row write:
  1. Claim `(session_id, client_message_id)` in a module-level `_inflight_client_ids: set`. If it is already claimed, treat the message as a duplicate of the in-flight send.
  2. Look up `MessageService.find_user_message_by_client_id(db, session_id, cid)`: role `user` and `message_metadata["client_message_id"].as_string() == cid`, ordered by `sequence_number`, `limit 1`.
  3. On a hit, return `{"action": "duplicate", "session_id": ..., "message_id": existing.id, "pending": existing.sent_to_agent_status == "pending"}`.
  4. Release the claim in a `finally` that wraps the whole remaining body.
- Stamp `client_message_id` into the metadata of **all three** user-message creation branches: command stream (`:1597-1603`), sync command (`:1649`) and LLM (`base_message_metadata`, `:1752-1840`).
- `ChannelIngestionService` passes `action="duplicate"` through unchanged and treats it as not initiating streaming.

### 4.3 A2A handling of `duplicate`
- `handle_message_send`: on `duplicate`:
  - If `pending` and not in flight (`get_state` is not `working` from the lock or the stream), call `SessionService.initiate_stream(...)` (D9).
  - Either way, enter the §2.4 wait loop as if `action == "streaming"`.
- `handle_message_stream`: on `duplicate`:
  - If `pending`, continue down the normal path: yield `working` and run the locked runner, which collects the pending row. No new row is written.
  - Otherwise, yield one status event from `task_store.get_turn_state(session_id, message_id)` and return. That event is `working` with `final=False`, or a terminal state with `final=True`.
- `A2ATaskStore.get_turn_state(session_id, user_message_id)`:
  - If no newer `user` row exists, return the session-level state from §2.3.
  - Otherwise the turn is over. Map the last agent row between the two user rows: `user_interrupted` → `canceled`, `aborted` → `failed`, anything else (or no row) → `completed`.

### 4.4 History echo
In `convert_session_messages_to_a2a`, user rows whose metadata has `client_message_id` get `Message.metadata["cinna.client_message_id"]`. Add a `CLIENT_MESSAGE_ID_KEY` constant.

### 4.5 Checks
`tests/api/a2a_integration/`, `tests/api/external/`, `tests/api/agents/sessions/`, `tests/api/server_channels/` (shared `send_session_message`)

---

## §5 Cross-cutting invariants

**Concurrency and ordering (P1).**
1. `message/stream` #1 stores its row, yields `working`, then starts the detached producer, which takes the lock (wait mode).
2. #2 arrives on the same session: it stores its row (`pending`), yields `working`, and its producer waits on the lock.
3. #1's turn collected only its own row, streams it, runs teardown and `after_teardown` inside the lock, then releases.
4. #2 acquires the lock, collects its row, streams and finishes normally.
5. If #2's row existed before #1 collected, #1 batches both rows. #2 then finds nothing pending and `after_teardown` emits a final `completed` (spec Q6).

- A disconnect from #1 while #2 waits changes nothing: the producer is not tied to the consumer.
- The web path (`process_pending_messages`), ACP and A2A now share the same lock and serialize across surfaces.
- MCP uses busy mode (`use_session_lock=True`) and still rejects on its own sessions only.
- `tasks/cancel` interrupts the running turn only; a queued turn still runs afterwards (§9 R5).

**Consumers of `SessionMessage.status` with the new `aborted` value**

| Consumer | Reads | Change? |
|---|---|---|
| `frontend/src/components/Chat/MessageBubble.tsx:295-297,582-593` | badge for `user_interrupted` / `error` | No: `aborted` renders as a plain message without a badge. Not broken. An optional "Stopped" badge is a separate UI follow-up. |
| `frontend/src/components/Chat/RecoverSessionModal.tsx:33` | `system` + `error` only | No |
| `frontend/src/components/Chat/MessageList.tsx:102-112`, `hooks/useSessionStreaming.ts:77` | `streaming_in_progress` | No. Improves: orphans stop showing as in-progress. |
| `services/sessions/session_service.py:854` (recovery trim) | `system` + `error` | No |
| `services/sessions/message_service.py:876` (`MessagePublic`) | passthrough `str` | No (no schema change) |
| `services/improvement/session_snapshot_service.py:248` | `status or "completed"` | No: `aborted` is passed through as-is, which is correct for a snapshot |
| `acp/agent.py:510-535` | writes `user_interrupted` / `error` for its own partial row, guarded on `streaming_in_progress` | No. The C3 cancel path finalizes first, so ACP's guard skips. Both outcomes are acceptable. |
| Channel delivery (`services/server_channels/*`) | never reads message status. It keys on terminal events and `AGENT_MESSAGE_ID_META_KEY` | No |
| Status repair Pass D (`status_repair_channels.py:192-209`) | `interaction_status` plus `ctx.sessions_in_motion` | No. The new pass records every session it clears in `sessions_in_motion`, so D skips it this tick. |
| `a2a_event_mapper` / `a2a_task_store` | new readers | P2/P3 |
| Platform-knowledge doc `env-templates/platform-knowledge-env/.../frontend_backend_agentenv_streaming.md:735` | value list | Docs (§7) |

**No migration.** New data lives in existing JSON columns (`message_metadata`, `session_metadata`) and the existing `status` string column. The dedupe lookup is bounded by one session's rows through the `(session_id, sequence_number)` index. The cost is O(messages in the session) JSON reads per A2A send that carries an id, which is acceptable at chat sizes. Trade-off: there is no DB-level uniqueness, so two processes racing on the same id could both insert. The in-flight claim covers the supported single-process deployment.

---

## §6 Test plan (test-writer)

Target group: `backend/tests/api/a2a_integration/` (README there; its conftest already patches `get_fresh_db_session`, collects background tasks and stubs the env). Pure logic goes in `backend/tests/unit/`. Status-repair passes go in `backend/tests/api/agents/sessions/`, which uses AGENT + FULL targets (see the warning at `tests/utils/fixtures.py:49-53`). No test exists for `tasks/get` or `message/send` state today.

**Disconnect harness.** Starlette's `TestClient` buffers SSE and cannot disconnect mid-stream. Drive the ASGI app directly:
1. Call `app(scope, receive, send)`, where `receive` returns the request body and then blocks.
2. After `send` has seen the first `http.response.body` frame, `receive` returns `{"type": "http.disconnect"}`.
3. Use a `StubAgentEnvConnector` stream gated on an `asyncio.Event`, so the turn is provably mid-flight at disconnect.
4. Await `wait_for_detached_a2a_turns(timeout)`.
5. Assert through the JSON-RPC API.

The detached task must outlive the request, so run the test inside one event loop (async test or a single portal).

- **T1 (P1)**
  - `tests/unit/test_a2a_stream_event_handler.py`: **replace** `test_stream_cancels_producer_on_client_disconnect` (`:237`) with `..._keeps_producer_running_on_disconnect`. Add these cases:
    - after the consumer detaches, the producer emits more than 10k events without blocking and the queue stays empty
    - the task is removed from `_DETACHED_A2A_TURNS` when done
    - `emit_final_state` is a no-op after a mapped final event
  - `test_a2a_crash_recovery.py`:
    - T1.1 disconnect mid-stream → turn completes → the agent row is finalized (`streaming_in_progress` false, full content) → `tasks/get` state is `completed`
    - T1.2 two `message/stream` calls on one `taskId` → the second yields `working` first, its env stream starts only after the first finished (assert on the stub's call order), and both end with a final event
    - T1.3 the second stream finds nothing pending (both rows batched) → final `completed`
    - T1.4 T1.1 repeated on `/external/a2a` (reuse fixtures from `tests/api/external/external_a2a_agent_test.py`)
  - Web regression: `tests/api/agents/sessions/agents_session_stream_concurrency_test.py` unchanged and green.
- **T2 (P2)**
  - `tests/unit/test_a2a_task_state_mapping.py`: table test of the 8 rules in §2.2, including their precedence.
  - `test_a2a_task_state.py`:
    - T2.1 idle session after a finished turn → `completed`
    - T2.2 turn in flight (gated stub) → `working`
    - T2.3 `tasks/cancel` mid-turn → `canceled`
    - T2.4 unanswered question → `input-required`
    - T2.5 T2.1 on `/external/a2a`
    - T2.6 `message/send` returns once the turn ends (stub finishes quickly; wall clock well under the 300 s cap) with state `completed`
    - T2.7 `message/send` with an error turn → `failed`
    - T2.8 v1.0 `GetTask` returns the same state
- **T3 (P3)**
  - `tests/api/agents/sessions/agents_stream_cancel_finalize_test.py`: cancel the task running `stream_message_with_events` after the first assistant event → row `status == "aborted"`, events kept, `streaming_in_progress` false. A cancel before any assistant event writes no row.
  - `tests/api/agents/sessions/agents_orphaned_stream_repair_test.py`, calling `repair_orphaned_streams(RepairContext(session=db))`, each row labelled with its heartbeat state (stale / fresh / legacy; the `stream_owner` verdicts below predate P3b):
    - `dead` + 3 min old → aborted, and the session `running` is cleared
    - `self` + registered stream → untouched
    - `self` + nothing registered + 3 min → aborted
    - `unknown` + 3 min → untouched
    - `unknown` + 121 min + session idle → aborted
    - an already finalized row → untouched
    - a session in `ctx.sessions_in_motion` with `unknown` → untouched
    - pass order: the scheduler list contains `orphaned_streams` between `sessions` and `input_tasks`
  - `test_a2a_message_state_history.py`:
    - history carries `cinna.message_state` for each of `complete`, `canceled`, `aborted` and `streaming`
    - an in-flight `tasks/get` includes live events beyond the last flush, and the DB row is not modified by the read
    - `tasks/get` of a session with an aborted last row → `failed`
- **T4 (P4)**, in `test_a2a_client_message_id.py`:
  - T4.1 `message/stream` with a `messageId` → history user row has `cinna.client_message_id`
  - T4.2 the same id resent after completion → no new row, env stub not called again, a single final `completed` event
  - T4.3 resent while running → one `working` event with `final=false`, no new row
  - T4.4 `message/send` resend → the same task, one user row
  - T4.5 T4.2 on `/external/a2a`
  - T4.6 blank or whitespace ids are never deduped (two sends → two rows)
  - T4.7 the same id in two different sessions → both stored
  - T4.8 a duplicate of a slash command does not re-execute it
  - T4.9 a duplicate of a stuck `pending` row re-drives delivery once (stub called once)
- Update `backend/tests/api/a2a_integration/README.md` with the new files and the disconnect harness.

---

## §7 Docs (documenter)

- `docs/application/a2a_integration/a2a_protocol/a2a_protocol.md`
  - Replace the **Task State Mapping** table (`:87-96`) with the §2.2 rules.
  - **SSE Streaming Event Flow**: add that a client disconnect does not stop the turn, that concurrent sends queue, and that a stream always ends with a final event.
  - **History Replay**: add `cinna.message_state`, `cinna.client_message_id` and the live-buffer merge.
  - Add an **Idempotency** rule (per-session `messageId` dedupe).
- `a2a_protocol_tech.md`: detached producer and registry, `LockedTurnRunner`, state probe inputs, `get_turn_state`, dedupe location.
- `docs/application/agent_sessions/agent_sessions.md` and `_tech.md`: message status `aborted`, finalize-on-cancel, `stream_heartbeat_at` (was the `stream_owner` stamp until P3b), `client_message_id` metadata.
- `docs/application/external_agent_access/external_agent_access.md`: one sentence saying the recovery contract (tasks/get polling, dedupe) applies unchanged to `/external/a2a`.
- `docs/system/status_repair/status_repair.md` and `_tech.md`: the new `orphaned_streams` pass, its ordering, the boot-stamp verdicts and `STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES`.
- `docs/flows/message_ingress.md` (`~:193`): the A2A stream path now runs under the shared session lock in wait mode.
- `docs/application/realtime_events/frontend_backend_agentenv_streaming.md` and its platform-knowledge mirror under `backend/app/env-templates/platform-knowledge-env/…/frontend_backend_agentenv_streaming.md:624,735`: add `aborted` to the status list.
- Run `python3 .cinna-core-kit/scripts/docs_index.py impact` and then `make check-docs`.

## §8 Regression groups

`tests/unit/`, `tests/api/a2a_integration/`, `tests/api/external/`, `tests/api/agents/sessions/`, `tests/api/agents/commands/` (command batches through the processor), `tests/api/server_channels/` (shared `send_session_message` and `stream_message_with_events`), `tests/api/acp_integration/` (shared lock and partial-row finalize), `tests/api/mcp_integration/` (processor in busy mode), `tests/architecture/`. Run them as separate foreground calls, each under 10 minutes.

## §9 Risks and open questions

- **R1: process-local state.** The lock, the live buffer and the in-flight claim are per process; `backend/Dockerfile:55` defaults to 4 workers. Orphan repair no longer depends on them (D10: DB heartbeat; in-process state is only a skip guard). Under several workers, `tasks/get` for a turn owned by a sibling depends on `interaction_status` and the stored row alone (no live-buffer merge). See R11/R12 for the lock and the claim.
- **R2: `STREAM_STARTED` lag.** The handler is a detached task. For a very short turn it could commit `running` after the teardown clear, leaving `working` until the orphan pass (self-owner, no lock, no local stream) clears it about 2 minutes later. This race already exists on the web path; the new pass now bounds it.
- **R3: misjudged stale heartbeat.** A live turn whose beats stall for 2+ min (DB outage, blocked loop) can be sealed `aborted` / cleared. It heals itself: the next beat or flush reopens the row, the next beat restores `running` on the session (marker written by the pass), and the final write sets `""`. Not healed: a session cleared by Pass B (no marker) after a 2 h stall, and a double race where the turn's own `STREAM_COMPLETED` clear lands before a restoring beat (the session then shows `running` until the heartbeat goes stale, about 2 to 4 min). The WS status event is not re-emitted on restore; the UI poll picks it up. The heartbeat ticker is off under pytest (shared test DB session); the ticker is unit-tested, the inline stamps run in API tests.
- **R4: cancel before the first assistant event** writes no agent row. `tasks/get` then reads `completed` rather than `canceled` (rule 6 has no row to read). Accepted; noted in the docs.
- **R5: queued turn after `tasks/cancel`.** A turn waiting on the lock still runs after cancel. Whether cancel should also drop the queued `pending` rows is left open for the desktop contract.
- **R6: first-turn resend without `taskId`** creates a new session and is not deduped, because dedupe is scoped to a session. The desktop must use the `taskId` from the first event (spec §6); C5 does not change this.
- **R7: shutdown.** Detached turns are cancelled when the process stops. C3 marks them `aborted`, which is the intended `failed` outcome. No graceful drain is added.
- **R8: test harness.** The disconnect scenario needs the raw ASGI driver described in §6. If that proves flaky, fall back to driving `handle_message_stream` directly and cancelling its consuming task, which mirrors Starlette's cancel.
- **R9: silent final in a batched turn.** A `message/stream` whose row was batched into an earlier turn finds nothing pending and emits a final state read from the session after teardown. If that earlier turn was aborted or canceled and its agent row is no longer the current turn's, the stream reports `completed`. Accepted; the client can read the per-message `cinna.message_state` from `tasks/get`.
- **R10: in-flight duplicate over SSE.** A `message/stream` resend that arrives while the first send is still being stored (claim held, no stored row yet) gets one non-final `working` event and the stream ends. The client polls `tasks/get` for the outcome.
- **R13: hung but connected env stream.** A stream whose environment stays connected but never sends a chunk keeps beating (httpx times out per chunk, 30 min). Pass B still reaps its `running` claim once `streaming_started_at` is older than `STATUS_REPAIR_STREAM_HARD_MAX_AGE_HOURS` (default 12), heartbeat or not. It writes no restore marker. The orphan pass's marker carries the cleared claim's original `streaming_started_at`, and a restoring beat puts that value back, so a misjudged clear never resets the hard-cap clock. The agent row stays `streaming_in_progress` until the turn ends, since sealing a row that still beats would only be reopened.
- **R14: rolling deploy.** A pre-D10 worker writes no heartbeat. `StreamHeartbeat.stop()` removes `stream_heartbeat_at` from the session at turn end (also on a cancelled stop, best-effort), compare-and-delete under the row lock: only while the key still holds this ticker's last stamp, so an overlapping turn on another worker (R11) keeps its stamp. This way a later pre-D10 turn is not judged by a finished turn's stamp. Residual: a turn whose process crashed (or whose clear failed) leaves a stale session stamp, so a pre-D10 worker's next turn on that session may be cleared after 2 min (only during the deploy window; a new worker's first beat overwrites the stamp). The agent row keeps no stamp after the final write, and a sealed row is no longer a candidate.
- **R11: cross-worker concurrent sends.** The per-session wait lock only serializes turns within one worker. Two sends for the same session landing on different workers may still overlap. This affects queueing only; documented, not fixed.
- **R12: cross-worker duplicate resend.** The `messageId` in-flight claim is per process, so two workers racing the same resend before the first row is stored can both run it. The DB lookup covers every later resend.
