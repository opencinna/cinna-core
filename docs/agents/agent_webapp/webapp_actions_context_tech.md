# Webapp Actions Context & Session Instructions — Technical Details

## File Locations

### Backend Services

- `backend/app/services/sessions/session_service.py` — `SessionService.activate_webapp_context()` method: checks and sets the session flag
- `backend/app/services/sessions/message_service.py` — one-time injection logic in the dispatch path; `send_message_to_environment_stream()` and `stream_message_with_events()` with `include_extra_instructions` / `extra_instructions_prepend` parameters

### Agent-Core (Inside Docker, Env-Template)

- `backend/app/env-templates/app_core_base/core/server/models.py` — `ChatRequest` model: `include_extra_instructions` and `extra_instructions_prepend` optional fields
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `_build_extra_instructions_block()` helper function; used in both `POST /chat` and `POST /chat/stream` handlers

### Env-Template Files

- `backend/app/env-templates/python-env-advanced/app/workspace/webapp/WEB_APP_ACTIONS.md` — workspace template file, copied to `/app/workspace/webapp/WEB_APP_ACTIONS.md` at environment initialization
- `backend/app/env-templates/general-env/app/workspace/webapp/WEB_APP_ACTIONS.md` — workspace template file for general-env, same content
- `backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md` — building agent prompt; contains the `## Maintaining the Actions Registry` section pointing to `/app/workspace/webapp/WEB_APP_ACTIONS.md`

## Database

### Session.session_metadata — New Flag

No new database tables or migrations. The flag lives in the existing JSON column on the `Session` model.

| Model | File | Field |
|-------|------|-------|
| `Session` | `backend/app/models/sessions/session.py` | `session_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))` |

**Key**: `"webapp_actions_context_sent"`
**Type**: `bool`
**Semantics**: Absent or `False` = injection not yet sent for this session. `True` = injection already sent, no further injection needed.

Follows the same pattern as `recovery_pending` (see `SessionService` and session recovery feature).

## Backend: session_service.py

### `SessionService.activate_webapp_context(db_session, session_id) -> bool`

```python
@staticmethod
def activate_webapp_context(db_session: DBSession, session_id: UUID) -> bool
```

Checks `session_metadata["webapp_actions_context_sent"]` on the session row.

- If the session is not found: returns `False` (safe default).
- If flag is already `True`: returns `False` (already active, no injection needed).
- If flag is absent or `False`: sets flag to `True`, calls `flag_modified(chat_session, "session_metadata")`, commits immediately via `db_session.commit()`, returns `True`.

The commit happens before the payload is sent to agent-core. This prevents a race condition where two concurrent messages for the same session both arrive before the flag is set, causing duplicate injections.

Logging: `[webapp_context]` prefix. Warning on session not found; info on flag activation.

## Backend: message_service.py

### Injection Detection — dispatch path in `collect_pending_messages()` + `process_pending_messages()`

Location: inside `process_pending_messages()`, after `collect_pending_messages()` resolves the pending message list.

```python
has_page_context = any(
    (msg.message_metadata or {}).get("page_context")
    for msg in pending_messages
)
if has_page_context:
    from app.services.session_service import SessionService
    should_inject = SessionService.activate_webapp_context(db, session_id)
    if should_inject:
        extra_instructions_prepend = (
            "This session is connected to a webapp that the user is viewing.\n"
            "- The user's current page state is included as <page_context> or "
            "<context_update> blocks in their messages.\n"
            "- You can interact with the webapp UI by embedding <webapp_action> "
            "tags in your responses (see webapp actions documentation for syntax and available actions)."
        )
        if session_mode == "conversation":
            include_extra_instructions = "/app/workspace/webapp/WEB_APP_ACTIONS.md"
```

The injection is **mode-aware**:
- **Conversation mode**: both `extra_instructions_prepend` (orientation) and `include_extra_instructions` (file path to `WEB_APP_ACTIONS.md`) are set. The conversation agent has no other way to learn about available actions — the file contents are inlined into the message.
- **Building mode**: only `extra_instructions_prepend` is set. `include_extra_instructions` stays `None`. The building agent already has the full actions documentation available through `WEBAPP_BUILDING.md` (in its system prompt), which references `ACTIONS_REFERENCE.md` and `WEB_APP_ACTIONS.md` for on-demand reading.

Both variables default to `None` and are passed through to `stream_message_with_events()`.

### `stream_message_with_events()` — Signature Extension

```python
@staticmethod
async def stream_message_with_events(
    session_id: UUID,
    environment_id: UUID,
    user_message_content: str,
    session_mode: str,
    external_session_id: str | None,
    get_fresh_db_session: callable,
    include_extra_instructions: str | None = None,
    extra_instructions_prepend: str | None = None,
) -> AsyncIterator[dict]
```

Forwards both fields to `send_message_to_environment_stream()`.

### `send_message_to_environment_stream()` — Payload Construction

```python
@staticmethod
async def send_message_to_environment_stream(
    base_url: str,
    auth_headers: dict,
    user_message: str,
    mode: str,
    external_session_id: str | None = None,
    backend_session_id: str | None = None,
    session_state: dict | None = None,
    include_extra_instructions: str | None = None,
    extra_instructions_prepend: str | None = None,
) -> AsyncIterator[dict]
```

Adds the fields to the HTTP payload only when non-`None`:
```python
if include_extra_instructions:
    payload["include_extra_instructions"] = include_extra_instructions
if extra_instructions_prepend:
    payload["extra_instructions_prepend"] = extra_instructions_prepend
```

## Agent-Core: models.py

### `ChatRequest` — New Optional Fields

```python
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    backend_session_id: Optional[str] = None
    mode: str = "conversation"
    system_prompt: Optional[str] = None
    session_state: Optional[dict] = None
    include_extra_instructions: Optional[str] = None
    # Absolute path to file whose contents are inlined into a one-time
    # <extra_instructions> block prepended to the message before the SDK call.
    # Generic/reusable — any feature can pass a different path. None = no injection.
    extra_instructions_prepend: Optional[str] = None
    # Optional static text prepended before the file contents inside the block.
```

## Agent-Core: routes.py

### `_build_extra_instructions_block(include_extra_instructions, extra_instructions_prepend) -> str | None`

Module-level helper. Fully generic — no webapp-specific logic.

```python
def _build_extra_instructions_block(
    include_extra_instructions: str | None,
    extra_instructions_prepend: str | None,
) -> str | None
```

Logic:
1. If both arguments are `None`: returns `None` immediately.
2. Reads `extra_instructions_prepend` into `parts` list if present.
3. Reads the file at `include_extra_instructions` path via `Path(path).read_text(encoding="utf-8")`:
   - `FileNotFoundError`: logs warning, skips file contents.
   - Other exceptions: logs warning with error detail, skips file contents.
4. If `parts` is empty after all attempts: returns `None`.
5. Joins parts with `"\n\n"`, wraps in `<extra_instructions>...\n</extra_instructions>`.

### Usage in Route Handlers

Both `POST /chat` (non-streaming) and `POST /chat/stream` (SSE) handlers:

```python
extra_block = _build_extra_instructions_block(
    request.include_extra_instructions,
    request.extra_instructions_prepend,
)
effective_message = (
    f"{extra_block}\n\n{request.message}" if extra_block else request.message
)
```

The `effective_message` (not `request.message`) is passed to `sdk_manager.send_message_stream()`. The streaming handler additionally logs the block character count at INFO level.

### Assembled Block Format

**Conversation mode** (orientation + file contents):

```
<extra_instructions>
This session is connected to a webapp that the user is viewing.
- The user's current page state is included as <page_context> or <context_update> blocks in their messages.
- You can interact with the webapp UI by embedding <webapp_action> tags in your responses (see webapp actions documentation for syntax and available actions).

[contents of /app/workspace/webapp/WEB_APP_ACTIONS.md]
</extra_instructions>

[user message content, including <page_context> or <context_update> blocks]
```

**Building mode** (orientation only — no file contents):

```
<extra_instructions>
This session is connected to a webapp that the user is viewing.
- The user's current page state is included as <page_context> or <context_update> blocks in their messages.
- You can interact with the webapp UI by embedding <webapp_action> tags in your responses (see webapp actions documentation for syntax and available actions).
</extra_instructions>

[user message content, including <page_context> or <context_update> blocks]
```

## Env-Template: WEB_APP_ACTIONS.md

### Template Location

`backend/app/env-templates/python-env-advanced/app/workspace/webapp/WEB_APP_ACTIONS.md`

This file is copied into the agent workspace at `/app/workspace/webapp/WEB_APP_ACTIONS.md` during Docker image build (same layer that places all workspace template files from `python-env-advanced/app/workspace/`).

### Template Structure

- `## Syntax` — `<webapp_action>{"action": ..., "data": {...}}</webapp_action>` block
- `## Built-in Actions` — table of five built-in action types with required data fields
- `## Custom Actions` — empty section with HTML comment marker; the building agent appends entries here

Five built-in actions documented in the template:

| Action | Required data fields |
|--------|---------------------|
| `refresh_page` | none |
| `reload_data` | `endpoint` (relative API path) |
| `update_form` | `form_id`, `values` (field-to-value map) |
| `show_notification` | `message`, `type` (success/error/warning/info) |
| `navigate` | `path` (relative URL) |

The file is agent-writable. The building agent is instructed to update it when implementing custom `webapp_action_*` event listeners.

## Env-Template: WEBAPP_BUILDING.md

### Added Section

`backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md` contains the `## Maintaining the Actions Registry` section immediately after `## Agent-to-Webapp Actions`:

```
## Maintaining the Actions Registry

The file `/app/workspace/webapp/WEB_APP_ACTIONS.md` is the actions registry for this agent.
It pre-documents the built-in action types. When you implement custom event handlers in your
webapp JavaScript (e.g., `window.addEventListener('webapp_action_my_action', ...)`), you MUST
add an entry to `WEB_APP_ACTIONS.md` describing the action type and what data it expects. This
keeps conversation mode informed about what custom actions are available to use.
```

This section is only seen by the building agent (it is part of the building mode system prompt via the prompts folder). Conversation mode never loads `WEBAPP_BUILDING.md`.

## Error Handling Summary

| Scenario | Component | Behavior |
|----------|-----------|----------|
| Session not found | `activate_webapp_context()` | Returns `False`; logs warning at `[webapp_context]` prefix |
| DB commit fails on flag set | `activate_webapp_context()` | Exception propagates; on next message flag absent → fires again (at most one duplicate) |
| `WEB_APP_ACTIONS.md` missing in container | `_build_extra_instructions_block()` | Logs warning; block assembled with prepend text only |
| File read error (permissions, encoding) | `_build_extra_instructions_block()` | Logs warning with error detail; same graceful fallback |
| `include_extra_instructions` is `None` | `_build_extra_instructions_block()` | Returns `None`; no block prepended; zero overhead |

---

*Last updated: 2026-03-10*
