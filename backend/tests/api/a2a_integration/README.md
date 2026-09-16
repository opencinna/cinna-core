# A2A Integration Tests

Tests the A2A (Agent-to-Agent) protocol flow end-to-end: agent creation, access token setup, streaming messages via JSON-RPC, session/task ID consistency, and AgentCard discovery.

## Autouse Fixtures (conftest.py)

Same infrastructure as `tests/api/agents/conftest.py` (session proxy, environment adapter stub, background task collector, external service mocks) plus a patch for `get_fresh_db_session` in `a2a.py` so the A2A request handler stays on the test transaction.

See `tests/api/agents/README.md` for fixture details.

## Test Utilities

| Helper | Source | Description |
|--------|--------|-------------|
| `_enable_a2a` | inline | Enable A2A via `PUT /agents/{id}` |
| `_create_access_token` | inline | Create scoped JWT token via access-tokens API |
| `_build_streaming_request` | inline | Build v1.0 `SendStreamingMessage` JSON-RPC payload |
| `_parse_sse_events` | inline | Parse `data:` lines from SSE response body |
| `StubAgentEnvConnector` | `tests/stubs/agent_env_stub.py` | Predefined agent-env streaming responses |

### Crash-recovery test files (C1–C4, `docs/plans/a2a_crash_recovery_plan.md`)

| File | Covers |
|------|--------|
| `test_a2a_crash_recovery.py` | C1: client disconnect survives, two `message/stream` calls on one taskId serialize via the per-session lock, `/external/a2a` parity |
| `test_a2a_task_state.py` | C2: `tasks/get` state rules end to end (idle/in-flight/canceled/input-required/error), `message/send` polling, v1.0 `GetTask` parity |
| `test_a2a_message_state_history.py` | C3: `cinna.message_state` in history (complete/canceled/aborted/streaming), in-flight `tasks/get` live-buffer merge without writing to the DB, `tasks/get` of a session with an aborted last row |
| `test_a2a_client_message_id.py` | C4: client `messageId` dedupe on both `message/stream` and `message/send`, blank-id and cross-session cases, slash-command and stuck-pending-row redrive |
| `tests/unit/test_a2a_crash_recovery_internals.py` | Unit: `A2AStreamEventHandler` detached-producer/`emit_final_state` edge cases, `LockedTurnRunner` teardown order/cancel, `is_session_lock_held` |
| `tests/unit/test_a2a_task_state_mapping.py` | Unit: `A2AEventMapper.map_session_status_to_task_state` decision table and rule precedence |
| `tests/api/agents/sessions/agents_stream_cancel_finalize_test.py` | C3: a real task cancel (not the interrupt API) seals the agent row `aborted` |
| `tests/api/agents/sessions/agents_orphaned_stream_repair_test.py` | C3: the `orphaned_streams` status-repair pass (boot-stamp `self`/`dead`/`unknown` verdicts) |

**Disconnect / concurrency harness (`tests/utils/a2a_raw_asgi.py`).** Starlette's `TestClient` buffers the whole SSE response and blocks the calling thread for the request, so it cannot produce a genuine mid-stream disconnect or run a second request truly concurrently with a still-open one. These tests instead drive the ASGI app directly (`await app(scope, receive, send)`) inside a single `asyncio.run(...)`, so a detached producer, a second overlapping request, or a manual background-task drain can all run on the same event loop. Key pieces:

- `start_a2a_sse` / `stream_a2a_sse_with_disconnect` — start (or start-and-disconnect-after-N-events) a `message/stream` SSE call.
- `post_a2a_json_at` — a quick JSON-RPC call (`tasks/get`, `tasks/cancel`) made while another gated turn is deliberately held open on the same loop.
- `post_a2a_json_draining` — for `message/send`'s internal poll loop: since the suite's `BackgroundTaskCollector` only *captures* the `process_pending_messages` coroutine (`drain_tasks()` would run it from the test thread, after the request already returned), this drives the request and drains that collector concurrently, in place, on the same loop.
- `GatedAgentEnvConnector` — an agent-env stub whose stream blocks until released, so a test can observe a turn genuinely "in flight" without racing a timer.

A detached producer only lives as long as the `asyncio.run(...)` call that created it — always `await wait_for_detached_a2a_turns(timeout=...)` (`app.services.sessions.stream_event_handlers`) before returning, or `asyncio.run()`'s own teardown cancels it mid-flight. This is a deliberate, narrow exception to the "no `app.services` imports in `tests/api/`" rule (`backend/tests/README.md` Rule 1): a detached producer is by design not observable through any endpoint, so there is no API-only way to wait for it.

**External-surface tests need one extra patch.** `tests/api/external/`'s own conftest additionally patches `app.api.routes.external_a2a.create_session` — `a2a_integration`'s conftest does not, since it has no reason to know about that route. A test here that reuses `tests/api/external/external_a2a_agent_test.py`'s helpers against `/external/a2a` must wrap the call in `with patched_create_sessions(db, ["app.api.routes.external_a2a.create_session"]):` (`tests/utils/fixtures.py`) or the external route's session lookups run outside the test transaction. See `test_a2a_crash_recovery.py::test_disconnect_mid_stream_completes_on_external_a2a` for the pattern.

## Related Documentation

- `docs/application/a2a_integration/a2a_protocol/a2a_protocol.md` — Architecture, data mapping, SSE event flow
- `docs/application/a2a_integration/a2a_protocol/a2a_v1_support.md` — v1.0 adapter layer and method name transformations
- `docs/application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md` — Token modes, scopes, and auth flow
