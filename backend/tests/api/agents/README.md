# Agent Tests

Agent tests exercise flows that depend on Docker environments, LLM streaming, and background services. The `conftest.py` in this directory provides autouse fixtures that stub all of these out.

## Topic Groups

This is the largest test domain in the suite (80 files / 592 tests), so tests are split into **topic group** subpackages. Every group inherits the autouse fixtures from this directory's `conftest.py` automatically — there is no per-group `conftest.py` and none should be added unless a group genuinely needs extra stubbing.

| Group | Covers |
|---|---|
| `bundles/` | Publishing: bundle + revision creation, workspace/metadata snapshots, publish settings, credential specs, template sharing, `service_uri`, per-user scope, permissions overview |
| `bundles_install/` | Installing and updating: install context, credential resolution/matching, readiness gate, auto-update convergence, scheduler propagation, admin env enrichment |
| `agent_api/` | Agent REST API: owner preview, connect helper, proxy + policy, caller identity & scopes, external keys, automatic-credential drift |
| `git/` | Git-backed agent versioning: checkout / pull / push, conflict resolution, baseline recovery, subdir-scoped update detection |
| `improvement_requests/` | Agent Improvement Requests: targeting, lifecycle, prompt/memory capture, signals, archive + scrubbing, rate limits, CLI surfaces |
| `schedules/` | Agent schedules: multi-schedule CRUD, schedule types + logs, manual "Run now" |
| `webapp/` | Agent webapp: share CRUD + public auth, serving, webapp chat (basic / actions / context), interface config, session instructions, `/webapp` command |
| `sessions/` | Session lifecycle and streaming: context, page context, recovery, reset, delete-interrupt, env detach, stream concurrency, message attachments |
| `commands/` | Slash / CLI command surface: `/run`, CLI command sync, autocomplete, `/files` + env wakeup, non-LLM → LLM bridging, agent status + status refresh |
| `guest_shares/` | Guest share links: CRUD, auth flow, security code, guest session access |
| `integrations/` | Inbound/outbound integrations: webhooks, capability flags, router trigger prompt |
| `core/` | Everything without a better home: create-flow, creation limits, prompt sync, resilient plugins, AI-credential slot matching, credential categorization, team task delegation, env token scoping, A2A access tokens |

**Placement rule.** New test files go into the group that matches their topic — never loose at the root of `tests/api/agents/`. Create a new group only for a genuinely new topic you expect to reach ~3 files; give it an `__init__.py` and add a row above. If `core/` grows past ~12 files, split it instead of letting it sprawl.

**Run scope.** Run the topic group, not the whole domain:

```bash
docker compose exec backend python -m pytest tests/api/agents/bundles_install/ -v
```

Run the full `tests/api/agents/` directory only when the change is cross-cutting — this directory's `conftest.py`, `tests/utils/fixtures.py`, or a shared service (session / message / environment lifecycle) that every group exercises.

## Autouse Fixtures (conftest.py)

### `patch_create_session`

Services create their own DB sessions via `create_session()`. This fixture replaces it with a `NonClosingSessionProxy` (from `tests/utils/db_proxy`) that returns the test `db` session, keeping all operations on the test transaction (rolled back after each test).

**Important**: Python's `from module import name` binds a local reference. Patching the source module alone doesn't update already-imported references. Every import site must be patched individually:

```python
patch("app.core.db.create_session", factory),
patch("app.services.sessions.session_service.create_session", factory),
patch("app.services.tasks.input_task_service.create_session", factory),
```

When a new service imports `create_session`, add its patch target here.

### `patch_asyncio_to_thread`

Runs `asyncio.to_thread` synchronously. Without this, threaded code would use a different connection outside the test transaction.

### `patch_environment_creation`

Replaces `EnvironmentService.create_environment` with `stub_create_environment` (from `tests/stubs/environment_stub.py`), which creates a DB record with `status="running"` and `is_active=True` — no Docker.

### `background_tasks`

Replaces `create_task_with_error_logging` at every import site (`session_service`, `event_service`) with a `_BackgroundTaskCollector`. Fire-and-forget coroutines (e.g. `process_pending_messages`, event handlers) are captured instead of scheduled on the event loop.

The collector is registered with `tests/utils/background_tasks.py` so that test utilities can drain collected tasks automatically via `drain_tasks()`. Tests do **not** need to interact with the collector directly.

Background tasks can't run inside the ASGI event loop (no nested `asyncio.run()`), so they are collected during API calls and drained from the test thread after the response returns. The drain loop handles cascading tasks (tasks spawned during execution of other tasks).

### `patch_external_services`

No-ops for external service calls:
- `CredentialsService.refresh_expiring_credentials_for_agent` — credential refresh
- `event_service.socketio_connector` — replaced with `StubSocketIOConnector` (captures emitted events, no real WebSocket server)

## Stubs

Located in `tests/stubs/`:

| Stub | Replaces | Usage |
|------|----------|-------|
| `StubIMAPConnector` | `imap_connector` | **No patch target right now.** `polling_service` no longer imports `imap_connector` (the per-agent email integration that drove it is deleted), so patching `app.services.email.polling_service.imap_connector` raises `AttributeError`. Patch it on whichever module imports it once the email channel transport lands under `app/services/server_channels/adapters/`; pass raw email bytes to the constructor |
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

1. Create `tests/api/agents/<group>/agents_<feature>_test.py` — pick the group from the
   "Topic Groups" table above; do not place the file at the root of `tests/api/agents/`
2. Use `client`, `superuser_token_headers`, and `db` fixtures
3. Set up data via API using helpers from `tests/utils/`
4. For task/streaming flows, use `ScriptedAgentEnvConnector` to simulate MCP tool calls mid-stream
5. Verify results via API using `get_agent_session`, `get_messages_by_role`, etc.
6. Verify session-driven status transitions — don't force status manually unless testing the status API itself
7. Use `db` for internal state not exposed via API
8. If your service imports `create_session`, add its patch target to `CREATE_SESSION_TARGETS_AGENT` in `tests/utils/fixtures.py`
9. If your service uses `create_task_with_error_logging`, add its patch target to `BACKGROUND_TASK_TARGETS_FULL`
10. If a test needs a workaround (manual status override, relaxed assertions), investigate whether the source code has a pattern violation (see "Source Code Invariants" in `backend/tests/README.md`)
