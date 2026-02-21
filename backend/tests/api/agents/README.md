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
| `StubAgentEnvConnector` | `agent_env_connector` | Patch `app.services.message_service.agent_env_connector`; yields predefined SSE events for agent streaming |
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

## Adding a New Agent Test

1. Create `tests/api/agents/agents_<feature>_test.py`
2. Use `client`, `superuser_token_headers`, and `db` fixtures
3. Set up data via API using helpers from `tests/utils/`
4. For email flows, use `process_emails_with_stub` with an `agent_env_stub`
5. Verify results via API using `get_agent_session`, `get_messages_by_role`, etc.
6. Use `db` for internal state not exposed via API (e.g. `OutgoingEmailQueue`)
7. If your service imports `create_session`, add its patch target to `conftest.py`
8. If your service uses `create_task_with_error_logging`, add its patch target to the `background_tasks` fixture
