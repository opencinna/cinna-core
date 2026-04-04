# Agent Tests

Agent tests exercise flows that depend on Docker environments, external mail servers, LLM streaming, and background services. The `conftest.py` in this directory provides autouse fixtures that stub all of these out.

## Autouse Fixtures (conftest.py)

### `patch_create_session`

Services create their own DB sessions via `create_session()`. This fixture replaces it with a `NonClosingSessionProxy` (from `tests/utils/db_proxy`) that returns the test `db` session, keeping all operations on the test transaction (rolled back after each test).

**Important**: Python's `from module import name` binds a local reference. Patching the source module alone doesn't update already-imported references. Every import site must be patched individually:

```python
patch("app.core.db.create_session", factory),
patch("app.services.email.processing_service.create_session", factory),
patch("app.services.session_service.create_session", factory),
```

When a new service imports `create_session`, add its patch target here.

### `patch_asyncio_to_thread`

Runs `asyncio.to_thread` synchronously. Without this, threaded code would use a different connection outside the test transaction.

### `patch_environment_creation`

Replaces `EnvironmentService.create_environment` with `stub_create_environment` (from `tests/stubs/environment_stub.py`), which creates a DB record with `status="running"` and `is_active=True` — no Docker.

### `background_tasks`

Replaces `create_task_with_error_logging` at every import site (`session_service`, `event_service`) with a `_BackgroundTaskCollector`. Fire-and-forget coroutines (e.g. `process_pending_messages`, event handlers) are captured instead of scheduled on the event loop.

The collector is registered with `tests/utils/background_tasks.py` so that test utilities (e.g. `process_emails_with_stub`) can drain collected tasks automatically via `drain_tasks()`. Tests do **not** need to interact with the collector directly.

Background tasks can't run inside the ASGI event loop (no nested `asyncio.run()`), so they are collected during API calls and drained from the test thread after the response returns. The drain loop handles cascading tasks (tasks spawned during execution of other tasks).

### `patch_external_services`

No-ops for external service calls:
- `CredentialsService.refresh_expiring_credentials_for_agent` — credential refresh
- `event_service.socketio_connector` — replaced with `StubSocketIOConnector` (captures emitted events, no real WebSocket server)

## Stubs

Located in `tests/stubs/`:

| Stub | Replaces | Usage |
|------|----------|-------|
| `StubIMAPConnector` | `imap_connector` | Patch `app.services.email.polling_service.imap_connector`; pass raw email bytes to constructor |
| `StubSMTPConnector` | `smtp_connector` | Patch `app.services.email.sending_service.smtp_connector`; assert on `.sent_emails` |
| `StubAgentEnvConnector` | `agent_env_connector` | Patch `app.services.message_service.agent_env_connector`; yields predefined SSE events for agent streaming. Use for simple response-only flows |
| `ScriptedAgentEnvConnector` | `agent_env_connector` | Patch same target; executes scripted MCP tool calls (real HTTP requests to TestClient) during the stream, then yields "done". Use when agent needs to call tools (create_subtask, add_comment, etc.) mid-stream. Only first `stream_chat` call runs scripted steps; subsequent calls use fallback. Track results via `.tool_results` and fallback count via `.fallback_call_count` |
| `StubSocketIOConnector` | `socketio_connector` | Applied automatically via conftest; captures emitted Socket.IO events |
| `stub_create_environment` | `EnvironmentService.create_environment` | Applied automatically via conftest |

IMAP, SMTP, and agent-env stubs are **not** autouse — patch them per-test or pass them to test utilities.

## Helpers

Located in `tests/utils/`:

| Helper | Description |
|--------|-------------|
| `create_agent_via_api(client, headers, name)` | Creates agent via POST API |
| `configure_email_integration(client, headers, agent_id, ...)` | Configures email integration for an agent |
| `enable_email_integration(client, headers, agent_id)` | Enables email integration |
| `create_imap_server(client, headers)` | Creates IMAP mail server config |
| `create_smtp_server(client, headers)` | Creates SMTP mail server config |
| `process_emails_with_stub(client, headers, agent_id, raw_emails, agent_env_stub)` | Polls IMAP, processes emails, and drains all background tasks (full pipeline) |
| `get_agent_session(client, headers, agent_id)` | Finds the single session for an agent via API |
| `get_messages_by_role(client, headers, session_id, role)` | Lists session messages filtered by role via API |
| `list_sessions(client, headers)` | Lists all sessions via API |
| `list_messages(client, headers, session_id)` | Lists all messages in a session via API |
| `execute_task(client, headers, task_id)` | Executes a task (creates session, sends message) |
| `get_task_sessions(client, headers, task_id)` | Lists sessions linked to a task |
| `agent_create_subtask(client, headers, task_id, ...)` | Agent creates subtask via `/agent/tasks/{id}/subtask` |
| `agent_create_subtask_current(client, headers, ..., source_session_id)` | Agent creates subtask via `/agent/tasks/current/subtask` |
| `agent_add_comment(client, headers, task_id, content, ...)` | Agent posts comment via `/agent/tasks/{id}/comment` |
| `agent_add_comment_current(client, headers, content, source_session_id)` | Agent posts comment via `/agent/tasks/current/comment` |
| `agent_update_status(client, headers, task_id, status)` | Agent updates task status (for `blocked`/`cancelled` only — completion should be session-driven) |
| `agent_get_task_details(client, headers, task_id)` | Agent reads task details via `/agent/tasks/{id}/details` |
| `agent_get_task_details_current(client, headers, source_session_id)` | Agent reads current task via `/agent/tasks/current/details` |

### `process_emails_with_stub`

This is the main helper for email integration tests. It:
1. Patches the IMAP connector with a stub containing the provided raw emails
2. Optionally patches `agent_env_connector` with the provided `agent_env_stub`
3. Calls the process-emails API endpoint
4. Drains all background tasks (process_pending_messages, event handlers, etc.)
5. Returns `(result_json, stub_imap)` for assertion

```python
stub_agent_env = StubAgentEnvConnector(response_text="Hello from agent")
result, stub_imap = process_emails_with_stub(
    client, superuser_token_headers, agent_id,
    raw_emails=[raw_email],
    agent_env_stub=stub_agent_env,
)
# Everything has completed — verify results via API
```

## Testing Task Delegation and MCP Tool Flows

For tests involving agent tasks, subtask delegation, or MCP tool calls during streaming, see the dedicated section in `backend/tests/README.md` → "Testing Session-Driven Flows".

Key patterns for this directory:

### Task Execution + Agent Streaming

```python
stub = StubAgentEnvConnector(response_text="I'll handle this.")
with patch("app.services.message_service.agent_env_connector", stub):
    exec_result = execute_task(client, headers, task_id)
    drain_tasks()  # streaming happens HERE, not during execute_task
```

### MCP Tool Calls During Stream (ScriptedAgentEnvConnector)

When an agent needs to call MCP tools (create_subtask, add_comment, etc.) during its session, use `ScriptedAgentEnvConnector`. It makes real HTTP calls to the backend mid-stream, just like the real SDK:

```python
stub = ScriptedAgentEnvConnector(
    client=client,
    auth_headers=headers,
    steps=[
        {"type": "assistant", "content": "Delegating work..."},
        {
            "type": "tool_call",
            "endpoint": f"/api/v1/agent/tasks/{task_id}/subtask",
            "method": "POST",
            "json": {"title": "Sub-task", "assigned_to": "Worker Agent",
                     "source_session_id": session_id},
            "tool_name": "mcp__agent_task__create_subtask",
        },
    ],
)
with patch("app.services.message_service.agent_env_connector", stub):
    drain_tasks()

# Verify tool call results
assert stub.tool_results[0]["status_code"] == 200
```

**Important:** Include `source_session_id` in `create_subtask` tool calls — this enables feedback delivery when the subtask completes, which is how the parent task's status gets re-synced.

### Session-Driven Status: Don't Force Completion Manually

After `drain_tasks()`, session completion event handlers automatically sync task status. Verify the automatic transition:

```python
# WRONG — bypasses the real completion flow
agent_update_status(client, headers, task_id=subtask_id, status="completed")

# RIGHT — session completion drives status automatically
with patch("...", stub):
    drain_tasks()
task = get_task(client, headers, subtask_id)
assert task["status"] == "completed"
```

Use `agent_update_status` only for testing the agent status API endpoint itself (e.g., `blocked`, `cancelled`), not as a workaround for missing session-driven completion.

### Multi-Agent Flows (Shared Patch)

When lead and worker agents stream in the same `drain_tasks()`, a single patched stub handles ALL `stream_chat` calls. `ScriptedAgentEnvConnector` runs scripted steps only on the first call — subsequent calls (worker auto-execute, feedback re-streams) use a simple fallback.

If you need different behavior per session, split the drains:

```python
# Phase 1: lead streams with scripted tool calls
with patch("...", lead_stub):
    execute_task(client, headers, task_id)
    drain_tasks()

# Phase 2: worker streams with its own scripted tool calls
with patch("...", worker_stub):
    drain_tasks()  # picks up auto-execute background task
```

## Adding a New Agent Test

1. Create `tests/api/agents/agents_<feature>_test.py`
2. Use `client`, `superuser_token_headers`, and `db` fixtures
3. Set up data via API using helpers from `tests/utils/`
4. For email flows, use `process_emails_with_stub` with an `agent_env_stub`
5. For task/streaming flows, use `ScriptedAgentEnvConnector` to simulate MCP tool calls mid-stream
6. Verify results via API using `get_agent_session`, `get_messages_by_role`, etc.
7. Verify session-driven status transitions — don't force status manually unless testing the status API itself
8. Use `db` for internal state not exposed via API (e.g. `OutgoingEmailQueue`)
9. If your service imports `create_session`, add its patch target to `CREATE_SESSION_TARGETS_AGENT` in `tests/utils/fixtures.py`
10. If your service uses `create_task_with_error_logging`, add its patch target to `BACKGROUND_TASK_TARGETS_FULL`
11. If a test needs a workaround (manual status override, relaxed assertions), investigate whether the source code has a pattern violation (see "Source Code Invariants" in `backend/tests/README.md`)
