# Channel Ingestion — Technical Reference

## File Locations

### Backend — Models
- `backend/app/models/sessions/session_sender.py` — `SessionSender` (frozen dataclass), `SessionSenderKind` (Literal), `ChannelAccessPolicy`, `IngestionResult`, `get_session_sender(session)` reader. Re-exported from `app.models.sessions.__init__` and `app.models.__init__`.

### Backend — Services
- `backend/app/services/sessions/channel_ingestion_service.py` — `ChannelIngestionService` (stateless), `NoActiveEnvironmentError`. Composes `SessionService` primitives; never re-implements message creation, stream initiation, or DB inserts.

### Backend — Channel callers (consumers of the service)
- `backend/app/services/a2a/a2a_request_handler.py` — core A2A surface; uses `ingest_inbound_message` for `message/send` and `resolve_or_create_session` for `message/stream` (streaming kicks via `SessionStreamProcessor`, not the service).
- `backend/app/services/external/external_a2a_context_handler.py` — External A2A surface (`A2ARequestHandler` subclass); overrides `_extra_session_kwargs` to thread `session_owner_id` through for non-owner target types.
- `backend/app/services/external/external_a2a_request_handler.py` — builds `TargetContext` per target type (`agent` → `session_owner_id=user.id`; `app_mcp_route` → `agent.owner_id` + separate `caller_id`; `identity` → `owner_id` + `identity_caller_id`).
- `backend/app/services/app_mcp/app_mcp_request_handler.py` — App MCP handlers (plain + identity routing); uses `assert_access` + `resolve_or_create_session`. Keeps legacy `MessageService.create_message` + `stream_and_collect_response` for message injection due to session-lock conflict.
- `backend/app/api/routes/sessions.py` — `POST /sessions`; uses `resolve_or_create_session` directly (no message body, first message lands via `/messages/stream` later).
- `backend/app/services/agents/agent_schedule_scheduler.py` — cron-fired and handover paths; uses `ingest_inbound_message` with `system_trigger` sender.
- `backend/app/services/tasks/input_task_service.py` — human-initiated task execution; uses `ingest_inbound_message` with `task_executor` sender.

### Backend — Architecture contract
- `backend/tests/architecture/channel_ingestion_callers_test.py` — Parametrized contract test enforcing the ≥2-callers rule per public method. Walks the repo, counts direct `ChannelIngestionService.<method>(` references via regex, excludes the definition site. Fails with an explicit message listing expected callers when the threshold drops below 2.

### Tests
- `backend/tests/unit/models/test_session_sender.py` — Unit tests for the value type (kinds, constructors, properties, round-trip `from_a2a` ↔ `get_session_sender`).
- `backend/tests/architecture/channel_ingestion_callers_test.py` — Contract test (above).
- `backend/tests/api/external/external_a2a_route_test.py::test_route_streaming_creates_app_mcp_session_with_correct_ownership` — End-to-end check that `app_mcp` target type stamps `session.user_id == agent.owner_id` and `session.caller_id == caller`.

## `SessionSender` Constructors

- `SessionSender.from_a2a(access_token_id: UUID | None, default_user_id: UUID)` — `kind="a2a_caller"`; `external_id = str(access_token_id) or str(default_user_id)`; `platform_user_id = default_user_id` (agent owner).
- `SessionSender.from_app_mcp(caller_user_id: UUID, identity_caller_user_id: UUID | None = None)` — `kind="mcp_caller"`; `platform_user_id = identity_caller_user_id or caller_user_id`.
- `SessionSender.from_webui(current_user: User)` — `kind="webui_user"`; `display_name = full_name or email`.
- `SessionSender.from_guest_share(context: GuestShareContext)` — `kind="anonymous"` when `context.user_id is None`, else `kind="webui_user"`.
- `SessionSender.from_task_execution(*, user_id, task_id, task_name)` — `kind="task_executor"`; `external_id = f"task:{task_id}"`.
- `SessionSender.from_system_trigger(*, owner_user_id, trigger_kind, trigger_id, display_name=None)` — `kind="system_trigger"`; `external_id = f"{trigger_kind}:{trigger_id}"`; `trigger_kind` is `Literal["schedule", "handover"]`.

## `ChannelIngestionService` Methods

- `ChannelIngestionService.ingest_inbound_message(*, db, agent, sender, thread_key, content, integration_type, access_policy, get_fresh_db_session, file_ids=None, backend_base_url=None, answers_to_message_id=None, extra_session_kwargs=None) -> IngestionResult` — full flow: `assert_access` → `resolve_or_create_session` → `SessionService.send_session_message` → map dict return to `IngestionResult`.
- `ChannelIngestionService.resolve_or_create_session(*, db, agent, sender, thread_key, integration_type, extra_session_kwargs=None) -> tuple[Session, bool]` — on resume verifies the sender matches via `_verify_resume_sender`; on create picks owner via `_select_session_owner_id` and runs `_stamp_new_session` for post-create extras. Raises `ValueError` when `thread_key` doesn't exist or `NoActiveEnvironmentError` when `SessionService.create_session` returns `None`.
- `ChannelIngestionService.assert_access(*, agent, sender, policy) -> None` — per-kind access dispatch. Raises `PermissionError` on denial.

### Internal helpers
- `_select_session_owner_id(*, agent, sender, extra_session_kwargs)` — kind-specific `Session.user_id` selection. Pops `session_owner_id` / `identity_owner_id` overrides off `extra_session_kwargs`.
- `_verify_resume_sender(existing, sender)` — kind-specific resume check. Raises `PermissionError` on mismatch or on disallowed kinds (e.g. `system_trigger`, `mcp_caller`).
- `_stamp_new_session(*, db, session, post_create_stamps)` — applies `_STAMPABLE_COLUMNS` (`caller_id`, `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`) plus `session_metadata_extra` merge. Raises `ValueError` on unknown keys.

### `_STAMPABLE_COLUMNS`
Tuple in `channel_ingestion_service.py`. Adding a new whitelisted post-create column requires updating this tuple + the corresponding caller(s).

## A2A Hook Protocol (subclassable extension points)

Defined on `A2ARequestHandler` (`backend/app/services/a2a/a2a_request_handler.py`); overridden by `ExternalA2AContextHandler`:

- `_parse_session_scope(task_id)` — parse + scope-check existing session id.
- `_authorize_existing_session(session)` — guard for `tasks/get` / `tasks/cancel`.
- `_stamp_new_session(session_id)` — post-create stamping that doesn't fit `extra_session_kwargs` (e.g. identity-binding columns + free-form metadata writes).
- `_integration_type_for_new_session() -> str | None` — value forwarded as `integration_type=`.
- `_extra_session_kwargs() -> dict | None` — merged into `extra_session_kwargs`. External handler returns `{"session_owner_id": context.session_owner_id}` unconditionally; the value differs by target type and is supplied by `_resolve_*_context` in `external_a2a_request_handler.py`.
- `_session_access_token_id() -> UUID | None` — threaded through both as the new-session lineage stamp and into `CommandContext` for slash commands.
- `_task_list_access_token_filter()` / `_task_list_filter(session)` — `tasks/list` filtering hooks.
- `_wrap_env_error(exc)` — shape env-readiness errors for the caller.
- `_stream_scope_error(exc, request_id)` — optional SSE-format for scope violations during streaming.

## `IngestionResult` shape

| Field | Type | Notes |
|---|---|---|
| `session` | `Session` | Always populated. |
| `message_id` | `UUID \| None` | Pass-through of `send_session_message`'s `message_id`. |
| `is_new_session` | `bool` | `True` when this call created the session; `False` on resume. |
| `streaming_initiated` | `bool` | `True` when `action in ("streaming", "pending")`. |
| `action` | `Literal["streaming", "pending", "queued", "command_executed", "error", "setup_required", "no_pending_messages", "message_created"] \| None` | Pass-through of `send_session_message`'s action mapping. |
| `message` | `str \| None` | Pass-through; carries error description for `"error"` and command response text for `"command_executed"`. |

## Reader: `get_session_sender(session) -> SessionSender`

Single place where the `integration_type → kind` mapping (writer ↔ reader symmetry) lives. Used for surfacing the sender on API responses, structured logging, and debugging — **never** for access control (channels build their own `SessionSender` via the constructors above).

Forward-compatible: unknown `integration_type` values fall back to `kind="platform_user"` rather than raising.

| `integration_type` | Reader-side `kind` | `external_id` derivation |
|---|---|---|
| starts with `"a2a"` | `a2a_caller` | `session.access_token_id` else `session.user_id` |
| `"app_mcp"` | `mcp_caller` | `session.caller_id` else `session.user_id` |
| `"identity_mcp"` | `mcp_caller` | `session.identity_caller_id` else `session.user_id` |
| `"task"` | `task_executor` | `f"task:{session_metadata.task_id}"` else `session.user_id` |
| `"schedule"` | `system_trigger` | `f"schedule:{session_metadata.schedule_id}"` else `session.user_id` |
| `None` | `webui_user` | `session.user_id` |
| any other (`"email"`, `"webhook"`, `"webapp"`, …) | `platform_user` | `session.user_id` |

## Configuration

No new settings keys, no env vars, no migrations. The service is pure addition over existing primitives.

## Security

- **Access checks are by sender kind**, not by `integration_type` (which is metadata). Channels never bypass `assert_access`.
- **`system_trigger` is not a fast-path skip** — it asserts `policy.expected_owner_id == agent.owner_id == sender.platform_user_id` as a structural invariant. A cron fire that mis-stamps the owner raises.
- **`task_executor` is a real owner-match check** — the executing human's `user_id` (passed through from the route) must equal `policy.expected_owner_id`. This replicates the pre-refactor check at `session_service.py` and makes the trust model visible.
- **Unknown post-create keys raise** instead of silently being dropped — typos in caller-side `extra_session_kwargs` surface as `ValueError` at the service edge.
- **App MCP routing is gated upstream.** `mcp_caller` access in the service is intentionally minimal (`policy.require_caller_in_route` sanity check only); the routing layer (`AppMCPRoutingService`) is the authoritative caller-to-agent gate.
