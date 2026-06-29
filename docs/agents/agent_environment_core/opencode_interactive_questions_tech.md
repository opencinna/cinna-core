# OpenCode Interactive Questions — Technical Reference

Implementation details for the OpenCode `question` / answer flow, the wedge bug, and the
`/reply`-relay fix. See [business doc](opencode_interactive_questions.md) for behavior.

## File Locations

**Agent environment core (inside container):**
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py`
  - `send_message_stream()` — the SSE loop + "first event triggers POST" handshake
  - `_handle`-side capture of the question request id from the DONE event (in the SSE loop,
    `OPENCODE_QUESTION_REQUEST_ID_KEY` read off the DONE metadata)
  - `finally` block — current behavior fires `_reject_question(...)` and cancels the POST task
  - `_reject_question(request_id)` — `POST /question/{id}/reject`
  - `_post_message(session_id, message)` — `POST /session/{id}/message` (the suspended turn)
  - `_create_session()` / `_delete_session()` — `POST /session` / `DELETE /session`
  - `_spawn_background(coro)` — strong-ref fire-and-forget helper (used for reject today)
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_event_transformer.py`
  - `_handle_question_asked()` — emits `askuserquestion` TOOL_USE + DONE; threads
    `opencode_question_request_id` through metadata
  - `OPENCODE_QUESTION_REQUEST_ID_KEY` constant
  - `question`-typed tool parts are suppressed (handled only via `question.asked`)
- `backend/app/env-templates/app_core_base/core/server/routes.py`
  - `chat_stream()` / `event_stream()` — env-core `/chat/stream` SSE endpoint that drives
    `sdk_manager.send_message_stream()`

**Host backend:**
- `backend/app/services/environments/agent_env_connector.py` — `stream_chat()` consumes the
  env-core SSE; `STREAM_TIMEOUT` (connect 30s / read 1800s / write 30s / pool 30s)
- `backend/app/services/sessions/message_service.py` — `stream_message_with_events()`;
  builds the `/chat/stream` payload (message, external `session_id`, `session_state`)
- `backend/app/services/sessions/session_service.py` — `interaction_status` lifecycle
  (`"running"` on STREAM_STARTED, cleared on STREAM_COMPLETED/INTERRUPTED)
- `backend/app/services/sessions/stream_event_handlers.py` — finalize handlers that clear
  `interaction_status`

**Config generation (host):**
- `backend/app/services/environments/environment_lifecycle.py` —
  `_generate_opencode_config_files()` writes `opencode.json`; the `permission` block
  (`"*": "allow"`, `external_directory`) and the `tools` enable map

## OpenCode HTTP contract (verified against opencode 1.14.x)

> All three endpoints below require the `?directory=/app/workspace` query param (the same
> workspace binding used by `/session*`). **Confirmed by the Phase 0 probe:** without it,
> `GET /question` returns `[]` even when questions are pending, and `/reply` cannot resolve.

- `GET /question?directory=/app/workspace` → array of pending `QuestionRequest` (`id`,
  `sessionID`, `questions`, `tool`). No server-side per-session filter param; filter
  client-side by `sessionID`.
- `POST /question/{requestID}/reply?directory=/app/workspace` — body `{ "answers": Answer[] }`,
  `Answer = string[]`, one entry per question in order; custom free-text labels allowed;
  empty padding slots (`[["Red"], []]`) are accepted. Resolves the pending Deferred; the
  suspended turn continues to a clean `session.idle`.
- `POST /question/{requestID}/reject?directory=/app/workspace` — fails the Deferred with
  `QuestionRejectedError`; the turn breaks/aborts.
- `GET /session/{id}/message?directory=/app/workspace` — used to inspect a session's last
  message/turn state when debugging.

OpenCode internals (from the bundled binary): `Question.ask` stores `{info, deferred}` in
the session `pending` map, publishes `question.asked`, then `T.ensuring(await(deferred),
delete-from-pending)` — so the entry is removed on **any** settle (reply, reject, or fiber
interruption). `Question.reply` looks up the entry by `requestID` and resolves it; a missing
id logs "reply for unknown request" and no-ops.

## Current (buggy) flow

1. `send_message_stream()` resumes the session and posts the user message via a background
   `_post_message` task; the SSE loop streams events.
2. On `question.asked`, `_handle_question_asked()` emits TOOL_USE(`askuserquestion`) + DONE;
   the loop captures the request id and returns.
3. `finally` calls `_reject_question(request_id)` (fire-and-forget) **and** cancels the
   suspended `_post_message` task.
4. The turn aborts and the OpenCode session is left non-finalized; the next
   `POST /session/{id}/message` (the user's answer as a normal message) hangs; the host
   stream is later torn down without clearing `interaction_status`.

## Fix design — `/reply` relay (parameter-free)

**Detection (per resume, before posting a message):**
- Add `_list_pending_question(session_id)` → `GET /question`, return the request whose
  `sessionID == session_id`.
- Keep an in-memory fast path `self._pending_questions: dict[str, dict]`
  (opencode_session_id → `{request_id, questions, ...}`), populated when `question.asked`
  is captured.
- If a pending question exists, the incoming message is the answer.

**Reply path:**
- Add `_reply_question(request_id, answers)` → `POST /question/{id}/reply` with `{answers}`.
- In the "first SSE event triggers the POST" handshake, fire `_reply_question(...)` instead
  of `_post_message(...)` when answering; then stream the resumed turn normally to
  DONE/`session.idle`.
- Map the message text to `answers`: structured selections if provided via `session_state`/
  metadata, else `answers = [[text]] + [[]] * (n_questions - 1)`.

**Lifecycle — SIMPLE end-then-reply (validated, implemented):**
- The Phase 0 probe confirmed the pending question **and** the suspended assistant turn
  **survive the originating POST disconnecting** (the turn fiber is decoupled from the POST
  socket): `GET /question` still lists the request and `/reply` resumes the turn to a clean
  `session.idle`. **Therefore the parked-POST / adapter-owned-session refactor is NOT
  needed** — the turn-N generator simply ends on `question.asked` and the next call replies.
- On `question.asked`, the adapter records the pending question and ends the stream; it does
  **not** reject. `_reject_question` is wired only into the explicit interrupt / cancel /
  `CancelledError` / `_delete_session` / `interrupt_session` teardown paths.

**Secondary (robustness, implemented):** `session_service.py` gained an idempotent,
never-raising `clear_interaction_status(session_id, reason)` (clears `interaction_status` +
`streaming_started_at`, dual-room WS emit), called from a shielded `finally` in
`message_service.py:process_pending_messages`, guaranteeing a cancelled/interrupted stream
can never leave the UI stuck "streaming".

## Config alternative (no relay)

Disable the blocking `question` tool in `_generate_opencode_config_files()` by adding an
explicit key (overrides the `"*": "allow"` wildcard): `permission.question = "deny"` or
`tools.question = false`. The model then asks in plain streaming text. Requires an env
rebuild/reconfigure (both `_generate_opencode_config_files` call sites run on create and
rebuild).

## Edge Cases

- **Multi-question single free-text answer:** ambiguous; text maps to question 0, the rest
  render "Unanswered". Structured widget answers are preferred for multi-question requests.
- **Stale/missing question** (OpenCode restarted, session gone): `GET /question` empty →
  fall back to a normal message post; never wedges because detection runs first.
- **Abandoned question** (user never answers / switches context): reject + clean up on
  session switch or teardown to avoid leaking the parked POST.
- **Transcript divergence:** the answer is recorded by OpenCode as the `question` tool
  result, not as a user message; the backend session still persists the user's typed
  message. Functionally consistent; relevant only to any future "rebuild OpenCode session
  from backend history" path.

## Tests

- Unit: `backend/tests/unit/test_opencode_event_transformer.py` — `question.asked`
  translation (TOOL_USE + DONE, request id threading).
- Add: answer-mapping (text → `answers`), pending-question detection, and an e2e
  question → answer → resumed-turn flow once the fix lands.

## Security

- Reuses the existing env-core auth on `/chat/stream`; the `/question/*` calls are made
  in-container to the local `opencode serve` (127.0.0.1:4096/4097), not exposed externally.
- Answer text is user-provided and flows to OpenCode as tool input; no new external surface.
