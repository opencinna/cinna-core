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

## Related Documentation

- `docs/a2a/a2a_integration.md` — Architecture, data mapping, SSE event flow
- `docs/a2a/a2a_v1_support.md` — v1.0 adapter layer and method name transformations
- `docs/a2a/agent_integration_access_tokens.md` — Token modes, scopes, and auth flow
