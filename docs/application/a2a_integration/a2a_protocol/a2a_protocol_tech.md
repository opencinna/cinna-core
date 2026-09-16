# A2A Protocol Integration - Technical Details

## File Locations

### Backend - API Layer
- `backend/app/api/routes/a2a.py` - A2A endpoints (GET AgentCard, POST JSON-RPC)
- `backend/app/api/main.py` - Router registration

### Backend - A2A Services
- `backend/app/services/a2a/a2a_service.py` - AgentCard generation + `apply_protocol()` v1.0 finalizer shared with `ExternalA2AService`
- `backend/app/services/a2a/a2a_request_handler.py` - Shared JSON-RPC dispatch + session/stream/task orchestration with hook methods for subclasses (default hooks enforce A2A access-token scope)
- `backend/app/services/a2a/a2a_event_mapper.py` - Centralized A2A protocol mapping logic
- `backend/app/services/a2a/a2a_task_store.py` - Task store adapter (uses service layer)
- `backend/app/services/a2a/a2a_v1_adapter.py` - A2A Protocol v1.0 adapter
- `backend/app/services/a2a/jsonrpc_utils.py` - Shared `resolve_protocol()`, `jsonrpc_success()`, `jsonrpc_error()` helpers used by both the `/a2a/` and `/external/a2a/` surfaces

### Backend - Core Services (used by A2A)
- `backend/app/services/sessions/session_service.py` - Session operations
- `backend/app/services/sessions/message_service.py` - Message operations
- `backend/app/services/sessions/stream_processor.py` - `SessionStreamProcessor` unified streaming pipeline; `LockedTurnRunner` (turn + teardown under the per-session lock); `is_session_lock_held()`
- `backend/app/services/sessions/stream_event_handlers.py` - `A2AStreamEventHandler` for A2A SSE event mapping; owns producer/consumer lifecycle via `stream(runner)`, `_run_processor()`, and `_enqueue_error_once()`; module-level `_DETACHED_A2A_TURNS` registry and `wait_for_detached_a2a_turns()`
- `backend/app/services/sessions/stream_heartbeat.py` - `StreamHeartbeat`, a per-turn DB heartbeat (`stream_heartbeat_at`) used to tell a live stream writer from an orphan left by a dead process, valid across backend workers
- `backend/app/services/agents/agent_service.py` - Skills generation integration

### Backend - AI Functions
- `backend/app/agents/skills_generator.py` - Skills extraction from workflow_prompt
- `backend/app/agents/prompts/skills_generator_prompt.md` - Prompt template

### Backend - Models
- `backend/app/models/agents/agent.py` - Agent model with `a2a_config` JSON field

### Backend - Migrations
- `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py` - Adds a2a_config field to Agent

### Frontend
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - A2A toggle + Agent Card URL display
- `frontend/src/components/Agents/A2aAccessTokensManager.tsx` - Access token management UI
- `frontend/src/routes/_layout/agent/$agentId.tsx` - Agent detail page with Integrations tab

### Test Client
- `backend/clients/a2a/run_a2a_agent.py` - Interactive A2A client for testing
- `backend/clients/a2a/utils.py` - A2A connection utilities and session logging
- `backend/clients/a2a/logs/` - Session log files (JSON format)

### Tests
- `backend/tests/api/a2a_integration/` - A2A integration tests
- `backend/tests/api/a2a_integration/test_a2a_cancel.py` - Integration tests for `CancelTask`: happy-path cancel verifies `forward_interrupt_to_environment` is called with the correct `external_session_id`; idempotent cancel verifies zero forwards; unknown-task cancel returns `-32001`
- `backend/tests/unit/test_a2a_stream_event_handler.py` - Unit tests for `A2AStreamEventHandler`: incremental delivery regression guard (measures inter-event gaps), sentinel on every exit path, cancel on disconnect, error deduplication, pre-stream errors
- `backend/tests/api/a2a_integration/test_a2a_crash_recovery.py` - Integration tests for the detached producer: a client disconnect does not abort the turn; driven through the raw ASGI harness below since `TestClient` cannot produce a genuine mid-stream disconnect
- `backend/tests/api/a2a_integration/test_a2a_task_state.py` - Integration tests for `tasks/get` state and the `message/send` poll end to end, including turn-genuinely-in-flight and cancel-mid-turn cases that need real concurrency
- `backend/tests/unit/test_a2a_crash_recovery_internals.py` - Unit tests for `A2AStreamEventHandler` drop-not-block at scale, `_DETACHED_A2A_TURNS` cleanup, `emit_final_state`'s no-op guard, `LockedTurnRunner` teardown ordering, and `is_session_lock_held`
- `backend/tests/unit/test_a2a_task_state_mapping.py` - Unit tests for `A2AEventMapper.map_session_status_to_task_state`'s rule order and precedence — pure logic, no DB or client
- `backend/tests/utils/a2a_raw_asgi.py` - Raw ASGI driver + gated agent-env stub, used where `TestClient` cannot simulate a mid-stream disconnect or true same-loop concurrency

## Database Schema

**Migration:** `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py`

**Model:** `backend/app/models/agents/agent.py`

**Field:** `Agent.a2a_config` (JSON) - Stores skills, version, generated_at, enabled flag

## API Endpoints

**File:** `backend/app/api/routes/a2a.py`

Three routers each expose the same endpoint patterns. Each router is registered separately in `api/main.py`.

| Endpoint | Method | Auth Required | Protocol | Description |
|----------|--------|---------------|----------|-------------|
| `/api/v1/a2a/{agent_id}/` | GET | Optional* | v1.0 (latest) | AgentCard in v1.0 format with versioned `supportedInterfaces` |
| `/api/v1/a2a/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v1.0 (latest) | AgentCard (standard location) |
| `/api/v1/a2a/{agent_id}/` | POST | Required | v1.0 (latest) | JSON-RPC, PascalCase method names |
| `/api/v1/a2a/v1.0/{agent_id}/` | GET | Optional* | v1.0 (explicit) | Identical to base URL |
| `/api/v1/a2a/v1.0/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v1.0 (explicit) | AgentCard (standard location) |
| `/api/v1/a2a/v1.0/{agent_id}/` | POST | Required | v1.0 (explicit) | JSON-RPC, PascalCase method names |
| `/api/v1/a2a/v0.3/{agent_id}/` | GET | Optional* | v0.3.0 (legacy) | AgentCard in v0.3 library-native format |
| `/api/v1/a2a/v0.3/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v0.3.0 (legacy) | AgentCard (standard location) |
| `/api/v1/a2a/v0.3/{agent_id}/` | POST | Required | v0.3.0 (legacy) | JSON-RPC, slash-case method names, no transformation |

\* Auth optional only when A2A is enabled - returns public card without auth, extended card with auth

**JSON-RPC Methods:**

| v1.0 Method (PascalCase) | v0.3 Method (slash-case) | Description |
|--------------------------|--------------------------|-------------|
| `SendMessage` | `message/send` | Synchronous message, returns Task |
| `SendStreamingMessage` | `message/stream` | SSE streaming response |
| `GetTask` | `tasks/get` | Get task status and history |
| `CancelTask` | `tasks/cancel` | Cancel running task |
| `ListTasks` | `tasks/list` | List tasks for agent (custom extension) |

Use PascalCase methods on `/a2a/` and `/a2a/v1.0/` URLs. Use slash-case methods on `/a2a/v0.3/` URLs.

## Services & Key Methods

### A2A Service
**File:** `backend/app/services/a2a/a2a_service.py`

- `A2AService.build_agent_card()` - Generates full (extended) AgentCard from Agent model
- `A2AService.build_public_agent_card()` - Generates minimal public AgentCard (name only)
- `A2AService.get_agent_card_dict(..., protocol="v0.3" | "v1.0")` - Returns full card as JSON-serializable dict, applying the v1.0 adapter when `protocol="v1.0"`
- `A2AService.get_public_agent_card_dict(..., protocol="v0.3" | "v1.0")` - Public card variant
- `A2AService.apply_protocol(card_dict, protocol)` - Public helper that runs the v1.0 outbound adapter; reused by `ExternalA2AService` for its synthesized identity card

### A2A Request Handler
**File:** `backend/app/services/a2a/a2a_request_handler.py`

Both `handle_message_send` and `handle_message_stream` route inbound calls through `ChannelIngestionService` for access enforcement, session resolution, and message injection — see [channel ingestion](../../agent_sessions/channel_ingestion.md) / [tech](../../agent_sessions/channel_ingestion_tech.md). The streaming path uses `resolve_or_create_session` only (stream kick is owned by `SessionStreamProcessor`); the non-streaming path uses the full `ingest_inbound_message`.

Shared dispatch methods (used by both the `/a2a/` surface and, via `ExternalA2AContextHandler`, the `/external/a2a/` surface):

- `A2ARequestHandler.handle_message_send()` - Non-streaming message handling; polls `task_store.get_state()` (cheap, no history read) once a second until a final/`input_required` state, then returns `task_store.get()`. On a duplicate `messageId` (see Idempotency below) it either re-drives delivery (`task_store.clear_delivery_failed()`) or joins the same poll loop as a fresh send. On an environment-readiness failure it flags this turn's own still-pending rows via `task_store.flag_pending_delivery_failed(session_id, own_max_sequence)`, `own_max_sequence` snapshotted (`task_store.get_max_user_sequence`) before the readiness check — the same mechanism `handle_message_stream`'s `_flag_undelivered` uses
- `A2ARequestHandler.handle_message_stream()` - SSE streaming handler; creates `A2AStreamEventHandler`, wraps the `SessionStreamProcessor` in a `LockedTurnRunner` (see Crash Recovery Implementation below), and iterates `handler.stream(runner)` to yield events incrementally to the client. A duplicate `messageId` whose original message is not still pending short-circuits before the lock is taken: it yields one status event from `task_store.get_turn_state()` and returns without running the turn again. When the resend races the original send before it has even been stored (an in-flight dedupe claim, `message_id` still `None`), `get_turn_state(session_id, None)` falls back to the session-level state — typically a non-final `working` — so the client is expected to fall back to polling `tasks/get` (plan §9 R10)
- `A2ARequestHandler.handle_tasks_get()` - Task query; merges `active_streaming_manager.get_stream_events(session_id)` (the live in-memory buffer) into `task_store.get_task_with_limited_history()` so a reconnecting client sees content not yet flushed to the DB
- `A2ARequestHandler.handle_tasks_cancel()` - Task cancellation; delegates to `MessageService.interrupt_stream()` so the interrupt is forwarded to the agent-env via HTTP, not merely flagged on the backend; treats "No active stream to interrupt" as idempotent success (terminal-task cancel per A2A spec); other errors (`ValueError`) propagate. Returns the `Task` via `_load_task()` (the same loader `handle_tasks_get()` uses, live-stream merge included), read after the interrupt; the dispatchers (`api/routes/a2a.py`, `ExternalA2ARequestHandler`) dump it like `tasks/get`, with the v1 transform on v1 requests
- `A2ARequestHandler.handle_tasks_list()` - List tasks (custom extension)

Hook methods — subclasses override to customize access control and session stamping. Default implementations enforce A2A-access-token scope (used by the core `/a2a/` route):

- `_parse_session_scope(task_id)` - Parse task_id UUID and enforce scope on existing sessions
- `_authorize_existing_session(session)` - Guard tasks/get and tasks/cancel
- `_stamp_new_session(session_id)` - Post-create hook (no-op default; external surface writes caller_id / metadata)
- `_integration_type_for_new_session()` - Integration type passed to `SessionService.send_session_message`
- `_session_access_token_id()` - Threaded into `SessionService` for both session lineage and `CommandContext`
- `_task_list_access_token_filter()` - DB-level `access_token_id` filter for tasks/list
- `_task_list_filter(session)` - In-memory filter for tasks/list results
- `_wrap_env_error(exc)` - Shape env-readiness errors for the caller
- `_stream_scope_error(exc, request_id)` - Optionally surface scope violations as inline SSE errors (default: propagate)
- `_extract_client_message_id(message_data)` - Reads `messageId`/`message_id`; keeps it only if it's a non-blank string of at most 255 characters, else `None` (logged at debug level)

Uses `SessionService` for session operations (no direct DB queries); streaming delegated to unified `SessionStreamProcessor`.

### A2A Event Mapper
**File:** `backend/app/services/a2a/a2a_event_mapper.py`

- `A2AEventMapper.map_stream_event()` - Internal streaming event to A2A event; handles `assistant`, `tool`, `thinking`, and `tool_result_delta` event types (mapped via `_STREAM_EVENT_TO_CONTENT_KIND`); stamps `cinna.command_invocation` on `tool` and `tool_result_delta` parts when the event carries a slash-command invocation
- `A2AEventMapper.map_session_status_to_task_state(status, interaction_status, tool_questions_status=None, *, turn_in_flight=False, has_pending=False, last_agent_status=None)` - Session status to TaskState; evaluates the rule table in [the business doc's Task State Mapping](./a2a_protocol.md#task-state-mapping) in order, first match wins. Callers not affected by the new keywords (e.g. `_final_if_silent`, see below) get identical behavior by omitting them
- `A2AEventMapper.is_terminal_task_state(state)` / `is_final_task_state(state)` - `TERMINAL_TASK_STATES` (`completed`, `failed`, `canceled`, `rejected`) and terminal-or-`input_required`, respectively. Used wherever a poll loop or a stream needs to decide "is this the end"
- `A2AEventMapper.map_stream_event()` - The `done` event maps to `canceled` when `metadata.interrupted`; otherwise, when `metadata.streaming_events` holds an ask-user tool call (`MessageService.detect_ask_user_question_tool`), to a final `input_required` status whose message carries those tool parts (`create_parts_status_update`); else `completed`
- `_parts_from_stream_events(events, session_id)` - Module helper shared by history replay and the `input_required` final event: one Part per mappable streaming event, with content-kind metadata
- `A2AEventMapper.convert_session_messages_to_a2a(messages, session_id, live_stream=None)` - SessionMessage list to A2A Message list. Stamps `MESSAGE_STATE_KEY` (`cinna.message_state`: `complete` / `streaming` / `aborted` / `canceled`) on agent messages and `CLIENT_MESSAGE_ID_KEY` (`cinna.client_message_id`) on user messages that carry one. When `live_stream` (the active-streaming-manager buffer) is given, the in-progress agent row's parts are built from `MessageService.merge_live_events(stored, live_stream["streaming_events"])` on **copies** — the ORM row itself is never mutated, since the caller's DB session would flush it
- `A2AEventMapper.create_status_update(part_metadata=...)` - low-level TaskStatusUpdateEvent construction; the optional `part_metadata` kwarg is attached to the embedded `TextPart` (not the `Message`) so streaming events carry content-kind metadata at part level
- `A2AEventMapper.create_notice_event(task_id, context_id, message)` - factory for a non-final `working` status update carrying `cinna.content_kind = "notice"` (env-activation hint and other ephemeral platform notices)
- `A2AEventMapper.create_command_result_event(task_id, context_id, message)` - factory for the terminal `completed` status update carrying `cinna.content_kind = "command_result"` and `cinna.command_invocation = "<slash invocation>"` (synchronous slash-command output yielded on the `command_executed` branch in place of the agent stream)
- `A2AEventMapper._build_parts_for_session_message(msg, role, events_override=None)` - Expands a stored agent `SessionMessage` into one `TextPart` per persisted streaming event, each carrying `cinna.content_kind` metadata; tool parts additionally carry `cinna.tool_name`, `cinna.tool_input` (when a dict), and `cinna.tool_id` (when non-empty) from the persisted event's `metadata`; `tool_result_delta` events are expanded into their own TextParts carrying `cinna.tool_id` and `cinna.tool_stream` metadata; `cinna.command_invocation` is preserved on replay whenever the persisted event stored it (tool, tool_result, and command_result parts from slash-command invocations); falls back to a single TextPart from `msg.content` when no trace is stored. `events_override`, when given, replaces the stored `streaming_events` for this call only — how `convert_session_messages_to_a2a` splices in the merged live buffer

#### Content-Kind Module-Level Constants

Defined at module level in `backend/app/services/a2a/a2a_event_mapper.py`; import from there rather than hardcoding string literals.

`tool_result` parts pair with their originating tool-call part by `cinna.tool_id` — the same identifier that appears on the `tool`-kind TextPart when the SDK emits a non-empty value.

| Constant | Value | Use |
|----------|-------|-----|
| `CONTENT_KIND_KEY` | `"cinna.content_kind"` | Metadata key placed on each `TextPart` |
| `MESSAGE_STATE_KEY` | `"cinna.message_state"` | `Message.metadata` key on agent rows in history: `complete` \| `streaming` \| `aborted` \| `canceled` |
| `CLIENT_MESSAGE_ID_KEY` | `"cinna.client_message_id"` | `Message.metadata` key on user rows in history, echoing the caller-supplied `messageId` when one was stored |
| `TOOL_NAME_KEY` | `"cinna.tool_name"` | Metadata key for tool name; present only on tool parts |
| `TOOL_INPUT_KEY` | `"cinna.tool_input"` | Metadata key for structured tool arguments (JSON object); present only on tool parts when the underlying SDK emits a dict |
| `TOOL_ID_KEY` | `"cinna.tool_id"` | Metadata key for opaque tool-call identifier string; present on tool parts when the underlying SDK emits a non-empty value, and on every tool_result part |
| `CONTENT_KIND_TEXT` | `"text"` | Value for `assistant` events (agent final answer) |
| `CONTENT_KIND_THINKING` | `"thinking"` | Value for `thinking` events (chain-of-thought) |
| `CONTENT_KIND_TOOL` | `"tool"` | Value for `tool` events (tool-call narration) |
| `CONTENT_KIND_TOOL_RESULT` | `"tool_result"` | Value for `tool_result_delta` events (CLI stdout/stderr from `/run:*` commands and LLM-side tool result chunks) |
| `CONTENT_KIND_NOTICE` | `"notice"` | Value for platform-emitted informational text — not part of the agent response. Used by the env-activation hint in `a2a_request_handler.handle_message_stream`; ephemeral (not persisted, not replayed via `GetTask`) |
| `CONTENT_KIND_COMMAND_RESULT` | `"command_result"` | Value for the terminal `completed` status event's message when the inbound request matched a platform slash command (e.g. `/files`, `/agent-status`). Set in `a2a_request_handler.handle_message_stream` on the `command_executed` branch — the agent stream does not run in that case |
| `TOOL_STREAM_KEY` | `"cinna.tool_stream"` | Metadata key for stdout/stderr classification; present only on tool_result parts |
| `TOOL_STREAM_STDOUT` | `"stdout"` | Default stream value; used when the underlying event omits a `stream` field or emits an unknown value |
| `TOOL_STREAM_STDERR` | `"stderr"` | Stream value for stderr output |
| `COMMAND_INVOCATION_KEY` | `"cinna.command_invocation"` | Cross-content-kind metadata key; present on `tool`, `tool_result`, and `command_result` parts when the part originated from a Cinna slash command. Value is the verbatim invocation string (e.g. `"/files"`, `"/run:rotate_status"`). Absent on LLM-initiated parts. |
| `CONTENT_KIND_FILE` | `"file"` | Value for `attachment` events (agent-authored file delivered as a native `FilePart`). The part is a `FilePart(FileWithUri)`, not a `TextPart`. Carries additional file-specific metadata keys: `cinna.file_id` (platform UUID), `cinna.file_name` (display filename), `cinna.file_mime` (sniffed MIME), `cinna.file_size` (bytes). `FileWithUri.uri` is a signed 1-hour download URL. See [agent_message_attachments_tech.md](../../../agents/agent_file_management/agent_message_attachments_tech.md) for the full part-contract table. |

Metadata is always placed on `TextPart.metadata` (for text parts) or `FilePart.metadata` (for file parts), never on `Message.metadata`, because a history-replay `Message` can contain parts of mixed kinds.

### A2A Task Store
**File:** `backend/app/services/a2a/a2a_task_store.py`

- `DatabaseTaskStore.get()` - Get task by ID (via SessionService)
- `DatabaseTaskStore.get_task_with_limited_history(task_id, history_length=10, live_stream=None)` - Get task with message limit; `live_stream` is forwarded to `A2AEventMapper.convert_session_messages_to_a2a` for the in-progress-row merge
- `DatabaseTaskStore.get_state(task_id, *, ignore_turn_in_flight=False)` - State only, no history read; cheap enough to poll every second. `ignore_turn_in_flight=True` skips the lock/stream/pending signals — used by `LockedTurnRunner`'s own teardown callback, where those signals describe the caller (the runner itself still holds the lock at that point), not the task
- `DatabaseTaskStore._map_status_to_state()` - Before the `interaction_status` rules (and after the unanswered-question rule) it returns `canceled` when `active_streaming_manager.is_interrupt_requested_nowait(session_id)` — a stop was requested for this process's running stream — unless `ignore_turn_in_flight`. A user row as the last message is judged by `_no_output_turn_status()`: `TURN_CANCELED_META_KEY` reads as a `user_interrupted` turn, `TURN_ABORTED_META_KEY` (`stream_heartbeat.py`, set by status repair Pass B2) as an `aborted` one
- `DatabaseTaskStore.get_turn_state(session_id, user_message_id)` - State of the turn a specific user message opened, not the session as a whole: while no newer user message exists this is the session-level state; once one does, the turn is over and the state comes from the last agent message between the two user messages (`user_interrupted` → `canceled`, `aborted` → `failed`, anything else → `completed`; no row → `canceled` / `failed` if the user message carries `TURN_CANCELED_META_KEY` / `TURN_ABORTED_META_KEY`, else `completed`). Used to answer a `message/stream` duplicate resend without re-running the turn
- `DatabaseTaskStore.is_turn_in_flight(session_id)` - `is_session_lock_held(str(session_id)) or active_streaming_manager.is_streaming_nowait(session_id)`
- `DatabaseTaskStore.clear_delivery_failed(message_id)` / `flag_pending_delivery_failed(session_id, up_to_sequence)` / `get_max_user_sequence(session_id)` - Thin wrappers over the `MessageService` methods of the same name, used by the duplicate-resend and locked-runner-failure paths respectively
- `_map_status_to_state()` queries no more than it must: the mapper's early-exit rules (tool question, `running`/`pending_stream`) are checked first, and the turn-in-flight / pending / last-agent-status queries only run if none of those already decided the state
- Delegates all A2A conversions to `A2AEventMapper`

### A2A v1.0 Adapter
**File:** `backend/app/services/a2a/a2a_v1_adapter.py`

- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names to internal (PascalCase → slash-case)
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard to v1.0 structure; builds distinct versioned URLs for `supportedInterfaces` by inserting `v1.0/` or `v0.3/` after `/a2a/` in the base URL
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator
- `A2AV1Adapter.transform_message_outbound(message)` - Add `kind` discriminator
- `A2AV1Adapter.transform_sse_event_outbound(event)` - Pass-through (already formatted)

The adapter is only applied for v1.0 and latest endpoints. The v0.3 endpoint bypasses the adapter entirely — the library speaks v0.3 natively.

### Skills Generator
**File:** `backend/app/agents/skills_generator.py`

- `generate_a2a_skills()` - AI-based skill extraction from workflow_prompt

### Integration with Existing Services

**SessionService:** `backend/app/services/sessions/session_service.py`
- `send_session_message(..., client_message_id=None)` - Creates session (if agent_id provided) + message, initiates streaming. When `client_message_id` and an existing `session_id` are both given, claims `(session_id, client_message_id)` in a process-local `_inflight_client_message_ids` set (closes the check/insert race), looks up `MessageService.find_user_message_by_client_id`, and returns `{"action": "duplicate", "message_id", "pending"}` before any command runs or row is written. On a miss it re-enters itself holding the claim, so the claim also covers the row insert
- `get_session()` - Get session by ID
- `list_environment_sessions()` - List sessions for environment with pagination and access token filter
- `update_interaction_status()` - Update interaction_status and pending_messages_count
- `auto_generate_session_title()` - AI-generated session titles from first message
- `ensure_environment_ready_for_streaming()` - Activate suspended environments

**MessageService:** `backend/app/services/sessions/message_service.py`
- `stream_message_with_events()` - Streams responses as internal events. It opens the agent row on the first content event (`_ROW_OPENING_EVENT_TYPES`: assistant, thinking, tool, tool_result, tool_result_delta), storing the events so far, so a tool-first turn that crashes still leaves its partial reply; the ~2 s periodic flush then follows. Its `finally` seals an in-progress row whenever the batch never reached its own finalize write — a cancel, a consumer going away (`CancelledError` / `GeneratorExit`), **or an error raised inside the stream itself**: `_seal_unfinished_turn` awaits (shielded) any flush already in flight, then calls `finalize_aborted_agent_message`, `user_interrupted=True` when `active_streaming_manager.is_interrupt_requested_nowait(session_id)` (the cancel followed a requested stop), else `False`. Before this, an error left the row `streaming_in_progress` forever; the error itself is still recorded as its own `system` message with `status="error"`, this only changes the partial agent row. Side effect: an ACP prompt error now shows its agent row as `aborted` too, since it runs through the same `finally`. When the batch wrote no agent row and a stop was requested (`was_interrupted` or the interrupt flag), the `finally` instead calls `mark_turn_canceled_before_output()`, which stamps `TURN_CANCELED_META_KEY` (`turn_canceled_at`) on the newest `sent` user row (rows queued behind are still `pending`)
- `interrupt_stream()` - Full interrupt flow: flag → resolve env → HTTP POST to agent-env `/chat/interrupt/{external_session_id}`; called by the UI route, webapp-chat route, and `A2ARequestHandler.handle_tasks_cancel`. Caller must authorize session access before calling (trust-based contract).
- `_apply_aborted(db, msg, events, content, *, user_interrupted=False)` / `finalize_aborted_agent_message()` - Locks the row (`with_for_update`), returns `False` without writing unless it is still `streaming_in_progress` (never overwrites a finalized row), then sets `status="aborted"` or `status="user_interrupted"`
- `has_pending_user_messages(db, session_id)` - EXISTS check on pending user rows, excluding those flagged `delivery_failed_at` (a turn that failed before collecting them — nothing is driving them, but they stay `pending` for the next turn)
- `get_last_agent_message_of_current_turn()` / `get_turn_closing_agent_message()` - Feed `A2ATaskStore._current_turn_agent_status` and `get_turn_state` respectively
- `find_user_message_by_client_id(db, session_id, client_message_id)` - Oldest user message stored with that `client_message_id`, the dedupe lookup `SessionService.send_session_message` runs
- `flag_pending_delivery_failed(db, session_id, up_to_sequence)` / `clear_delivery_failed(db, message_id)` - Stamp/clear `message_metadata["delivery_failed_at"]` on still-`pending` rows a failed turn never collected. `up_to_sequence` (`get_max_user_sequence(db, session_id)`, captured before the failing turn starts) scopes the flag to that turn's own rows, so rows of a turn already queued behind it are left alone
- `get_max_user_sequence(db, session_id)` - Sequence number of the session's newest user message (0 if none); the `up_to_sequence` snapshot for the call above
- `merge_live_events(db_events, live_events)` - Pure helper: appends live events whose `event_seq` exceeds the highest stored one. Shared by `_flush_streaming_to_db` and `A2AEventMapper.convert_session_messages_to_a2a`
- `get_last_message()` - Get last message (for tool_questions_status check)
- `get_last_n_messages()` - Get message history

**AgentService:** `backend/app/services/agents/agent_service.py:update_agent()`
- Detects workflow_prompt changes, triggers skills regeneration
- Updates a2a_config with new skills and incremented version

### A2A Stream Event Handler

**File:** `backend/app/services/sessions/stream_event_handlers.py`

`A2AStreamEventHandler` owns the producer/consumer lifecycle for A2A SSE streaming:

- `on_event(event)` - Maps each agent-env event to A2A format via `A2AEventMapper` and calls `_emit` to put it on an `asyncio.Queue`; sets `final_emitted=True` when the mapped event is itself final
- `on_error(error)` - Calls `_enqueue_error_once` to enqueue a final `failed` status event
- `emit_final_state(state)` - No-op if `final_emitted` is already set; otherwise emits one closing status update (`final=True` for a terminal state or `input_required`) and sets the flag. Used by `LockedTurnRunner`'s `after_teardown` callback when the turn produced no events of its own — e.g. its message was collected and answered as part of an earlier batched turn
- `_emit(item)` - Puts `item` on the queue via `put_nowait` (the queue is unbounded on purpose — the producer can never stall on a slow or absent consumer) unless `_consumer_attached` is `False`, in which case the item is silently dropped
- `stream(runner)` - Async iterator that runs `runner.process()` (a `LockedTurnRunner`, see Crash Recovery Implementation below) as a **detached** `asyncio.create_task`, held in the module-level `_DETACHED_A2A_TURNS` set so it survives past the consumer even though `asyncio` itself only keeps a weak reference to a task. Drains the queue and yields each SSE string to the caller; a done-callback discards the task from the registry and posts a `None` sentinel to guarantee the consumer unblocks on every exit path (normal completion, error, client disconnect, abrupt task death). On a client disconnect (`GeneratorExit`), the `finally` sets `_consumer_attached=False` and drains any events already queued — it does **not** cancel the producer; the turn runs to completion regardless of the SSE connection
- `_run_processor(runner)` - Wraps `runner.process()`; re-raises `CancelledError` without enqueuing a spurious `failed` event (only process shutdown cancels a detached producer); maps `ValueError`/`RuntimeError` to `_enqueue_error_once`
- `_enqueue_error_once(message)` - Emits a final `failed` status event at most once (guarded by `error_enqueued` flag, which also sets `final_emitted`) to prevent duplicate error events when both `on_error` and `_run_processor`'s except branch fire
- `wait_for_detached_a2a_turns(timeout)` (module-level) - Awaits a snapshot of `_DETACHED_A2A_TURNS` up to `timeout` seconds; never raises on timeout. Used by tests (and available for a future shutdown drain)

### Crash Recovery Implementation (C1–C4)

- `LockedTurnRunner` (`stream_processor.py`) - Wraps a processor in the per-session lock, **wait mode** (a queued turn waits, never rejected), plus teardown: a shielded `SessionService.clear_interaction_status()` runs inside the lock in every case. `after_teardown` (only on a normal return) is `_final_if_silent` in `handle_message_stream` — it calls `task_store.get_state(..., ignore_turn_in_flight=True)` and `handler.emit_final_state(...)`. Known gap (plan §9 R9): this reads the *session's* state, not the batch's — a message batched into an earlier turn that was aborted/canceled can be reported `completed` here once that turn's agent row is no longer the current one. `after_failure` (only when the turn raised) is `_flag_undelivered` — it calls `task_store.flag_pending_delivery_failed(session_id, own_max_sequence)`, where `own_max_sequence` is snapshotted before the turn starts so only this turn's own rows are flagged, not ones queued behind it. Flagged rows don't count as in-flight for `tasks/get`, but stay `pending` for the next turn. The wrapped processor must be built `use_session_lock=False`. The lock (`_session_locks`) is a **process-local** dict: it serializes concurrent sends to one session only within the worker that holds it, so two sends to the same session landing on different backend workers can still stream concurrently instead of queuing behind each other (plan §9 R11, documented, not fixed)
- `is_session_lock_held(session_id)` (`stream_processor.py`) - Read-only probe (`_session_locks.get(session_id)` + `.locked()`); never creates a lock entry, so probing an unknown session doesn't grow the registry
- `stream_heartbeat.py` (new; plan D10) - `StreamHeartbeat(session_id, get_fresh_db_session)`: `start()` / `attach_message(message_id)` / `stop()`. A per-turn ticker that writes `stream_heartbeat_at` (`STREAM_HEARTBEAT_KEY`) into `session_metadata` from turn start, and into the in-progress agent row's `message_metadata` once it exists, roughly every `HEARTBEAT_INTERVAL_SECONDS` (30s); also stamped on message creation and every periodic flush. Because the heartbeat is DB state, the status-repair orphan pass's verdict holds across every backend worker, not just the one that started the turn — see [status repair tech](../../../system/status_repair/status_repair_tech.md) for the pass and the self-heal marker (`ORPHAN_CLEARED_KEY`). The client `messageId` in-flight dedupe claim, by contrast, is still process-local (`services/sessions/session_service.py`): two resends racing the same `messageId` before the first row is stored can both run if they land on different workers (plan §9 R12) — every resend after that first row exists is deduped by the stored `messageId` regardless of worker

## Frontend Components

**AgentIntegrationsTab:** `frontend/src/components/Agents/AgentIntegrationsTab.tsx`
- Toggle switch to enable/disable A2A (`a2a_config.enabled`)
- Read-only input displaying the Agent Card URL
- Copy button to copy URL to clipboard

**Agent detail page:** `frontend/src/routes/_layout/agent/$agentId.tsx`
- Integrations tab registered between Configuration and Credentials tabs

## Protocol Reference & Discovery

How to inspect A2A protocol types, required fields, and card structure when doing protocol analysis or upgrading to a newer spec version.

### Authoritative Sources

| Source | What it tells you | How to access |
|--------|-------------------|---------------|
| A2A v1 spec | Normative field definitions, required/optional, semantics | https://a2a-protocol.org/latest/specification/ — sections 4.4.x cover Agent Discovery Objects |
| `a2a-sdk` Python library | Pydantic models used at runtime; `Field(...)` = required, `Field(default=...)` = optional | Installed at `backend/.venv/lib/python3.13/site-packages/a2a/types.py` |
| Our v1 adapter | What we actually transform and emit for v1 clients | `backend/app/services/a2a/a2a_v1_adapter.py` |
| Live card output | The JSON a client actually receives | `curl http://localhost:8000/api/v1/a2a/{agent_id}/` (v1.0) or `curl http://localhost:8000/api/v1/a2a/v0.3/{agent_id}/` (v0.3) |

### Inspecting the Library Models

The `a2a-sdk` package (pinned `>=0.3.22` in `backend/pyproject.toml`) ships Pydantic v2 models in `a2a.types`. These are the source of truth for what our code can construct.

**List all fields and required status for any A2A type:**

```bash
source backend/.venv/bin/activate
python3 -c "
from a2a.types import AgentCard  # or AgentInterface, AgentProvider, AgentSkill, etc.
for name, field in AgentCard.model_fields.items():
    req = field.is_required()
    print(f'{name:40s} required={req}  default={field.default if not req else \"---\"}')
"
```

**Read the full class source (docstrings, field descriptions):**

```bash
python3 -c "
from a2a.types import AgentCard
import inspect
print(inspect.getsource(AgentCard))
"
```

### AgentCard Required Fields (a2a-sdk 0.3.22)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | `str` | Yes | Agent display name |
| `description` | `str` | Yes | Human-readable purpose description |
| `url` | `str` | Yes | Preferred endpoint URL (v0.3 top-level; v1 moved to `supportedInterfaces`) |
| `version` | `str` | Yes | Agent's own version number |
| `capabilities` | `AgentCapabilities` | Yes | Streaming, push notifications, extensions |
| `skills` | `list[AgentSkill]` | Yes | Agent capabilities (can be empty `[]`) |
| `defaultInputModes` | `list[str]` | Yes | MIME types for input |
| `defaultOutputModes` | `list[str]` | Yes | MIME types for output |
| `provider` | `AgentProvider` | No | Organization name + URL (sub-fields `organization` and `url` are both required when present) |
| `protocolVersion` | `str` | No | Default `"0.3.0"` |
| `preferredTransport` | `str` | No | Default `"JSONRPC"` |
| `securitySchemes` | `dict[str, SecurityScheme]` | No | OpenAPI 3.0 security scheme objects |
| `security` | `list[dict]` | No | Security requirement objects |
| `additionalInterfaces` | `list[AgentInterface]` | No | Extra transport/URL combos |
| `supportsAuthenticatedExtendedCard` | `bool` | No | Signals extended card availability |
| `documentationUrl` | `str` | No | Link to agent docs |
| `iconUrl` | `str` | No | Agent icon |
| `signatures` | `list[AgentCardSignature]` | No | Card signing (JWS) |

### Key Nested Types

**AgentInterface** — declares a transport + URL pair:
- `url` (str, required) — endpoint URL
- `transport` (str, required) — e.g. `"JSONRPC"`, `"GRPC"`, `"HTTP+JSON"`

**AgentProvider** — service provider info:
- `organization` (str, required) — provider org name
- `url` (str, required) — provider website

**AgentSkill** — agent capability:
- `id` (str, required), `name` (str, required), `description` (str, required)
- `tags` (list[str], optional), `examples` (list[str], optional)

### v0.3 vs v1 Card Output

Each protocol version has its own dedicated URL. The format is determined by the URL, not any request header.

**v0.3 (legacy / library native) — GET `/api/v1/a2a/v0.3/{agent_id}/`:**
- `protocolVersion`: `"0.3.0"` (string)
- `url`: points to `/api/v1/a2a/v0.3/{agent_id}/` (v0.3-specific URL)
- `supportsAuthenticatedExtendedCard`: top-level bool

**v1.0 (default, via `A2AV1Adapter.transform_agent_card_outbound`) — GET `/api/v1/a2a/{agent_id}/` or `/api/v1/a2a/v1.0/{agent_id}/`:**
- `protocolVersions`: `["1.0", "0.3.0"]` (array)
- `supportedInterfaces`: distinct versioned URLs per protocol:
  ```json
  [
    {"url": "http://host/api/v1/a2a/v1.0/{id}/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    {"url": "http://host/api/v1/a2a/v0.3/{id}/", "protocolBinding": "JSONRPC", "protocolVersion": "0.3.0"}
  ]
  ```
- `capabilities.extendedAgentCard`: `true` (replaces `supportsAuthenticatedExtendedCard`)

### Quick Protocol Check Commands

```bash
# Fetch v1 card (default / latest)
curl -s http://localhost:8000/api/v1/a2a/{agent_id}/ | python3 -m json.tool

# Fetch explicit v1.0 card
curl -s http://localhost:8000/api/v1/a2a/v1.0/{agent_id}/ | python3 -m json.tool

# Fetch v0.3 card (legacy)
curl -s http://localhost:8000/api/v1/a2a/v0.3/{agent_id}/ | python3 -m json.tool

# Compare our output against spec field list
source backend/.venv/bin/activate
python3 -c "
from a2a.types import AgentCard
spec_required = {n for n, f in AgentCard.model_fields.items() if f.is_required()}
print('Required by library:', sorted(spec_required))
"
```

## Configuration

- **Prompt Template:** `backend/app/agents/prompts/skills_generator_prompt.md`
- **Dependencies:** a2a-sdk (`>=0.3.22` in `backend/pyproject.toml`, installed as `a2a-sdk`)
- **Agent Card URL:** `{VITE_API_URL}/api/v1/a2a/{agent_id}/`
- **Library types location:** `backend/.venv/lib/python3.13/site-packages/a2a/types.py`

## Security

- JWT authentication required for JSON-RPC endpoints
- Agent ownership validation (user must own agent or be superuser)
- Environment validation (agent must have active environment)
- JSON-RPC error codes for authorization failures (-32004)
- Public card exposes only name and URL (no skills/description)
- Security schemes included in both public and extended cards (Bearer JWT)
- Supports both user JWT tokens and A2A access tokens (see [A2A Access Tokens tech](../a2a_access_tokens/a2a_access_tokens_tech.md))

### SSE Response Format

Each SSE event is a JSON-RPC response containing a `TaskStatusUpdateEvent`:
- Events include `taskId`, `contextId`, `status` (with `state` and `timestamp`), and `final` flag
- Status updates may include a `message` with agent content
- Stream ends with a final event where `final=true`
- Format handled by `A2ARequestHandler._format_sse_event()` wrapping events in JSON-RPC response structure

## Sharing the Runtime with External Agent Access

`A2ARequestHandler`'s dispatch bodies (`handle_message_send`, `handle_message_stream`, `handle_tasks_*`) are reused by the first-party `/api/v1/external/a2a/` surface via the `ExternalA2AContextHandler` subclass in `backend/app/services/external/external_a2a_context_handler.py`. That subclass overrides the hook methods listed above to:

- Enforce caller-scope per `TargetContext.integration_type` (`external`, `app_mcp`, `identity_mcp`) instead of A2A-token scope
- Stamp `caller_id` / `identity_caller_id` / `session_metadata` on new sessions
- Re-check identity-binding validity on resume
- Raise `app.services.external.errors` domain exceptions instead of `ValueError`

The two feature surfaces keep their own routes, auth contexts, card builders, and access policies; only the protocol runtime is shared. See [External Agent Access](../../external_agent_access/external_agent_access.md) for the per-target-type rules on top of this shared dispatch.

---

*Last updated: 2026-09-16 — A2A crash recovery (C1–C4): detached producer, tasks/get in-flight state, cancel finalize + orphan-stream seal, client messageId dedupe; cancel returns the Task, question turns end input-required*
