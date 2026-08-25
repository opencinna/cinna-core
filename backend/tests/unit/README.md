# Unit Tests

Pure unit tests — isolated logic with **no database and no `TestClient`**. The conftest here
(`conftest.py`) overrides the root `setup_db` fixture with a no-op so these tests run without a
Postgres connection, and adds the env-template tree (`app/env-templates/app_core_base`) to
`sys.path` so adapter / MCP-bridge modules import cleanly.

## What belongs here

- Pure logic with no I/O: event transformers, parsers, decision tables, similarity / scoring
  functions, URL and filesystem-path helpers. Public ones too, when the module is pure — e.g.
  `app.services.routing.text_similarity`, covered by `test_text_similarity.py`.
- Private `_helper` functions extracted from services (e.g. `_parse_prompt_line`,
  `_assemble_session_prompt`). Being private is what makes a unit test the *only* place these
  can be covered; it is not what qualifies them. When a private helper acquires a second
  caller outside its owning service, promote it to a public module of its own rather than
  reaching across the boundary for an underscored name — `text_similarity` is that move having
  already happened, which is why it is an example on the bullet above and not this one.
- Egress-guard predicates (`validate_external_endpoint_url`, `is_host_blocked`) and other
  defensive checks.
- MagicMock-driven defensive-branch tests for code paths that have no clean API surface.
- Assertions about module-level constants (e.g. `AGENT_ENV_ALLOWED_FIELDS`, `SENSITIVE_FIELDS`).

Unlike `tests/api/`, importing from `app.services`, `app.core`, adapters and env-template modules
is **expected and allowed** here.

## What does NOT belong here

Anything that needs a real HTTP round-trip (`client` / `TestClient`), the full app lifespan, or
background-task draining. Those are integration tests — put them in `tests/api/<domain>/`.

**Litmus test:** if a "unit" test secretly needs `client`, it is not a pure unit test. Move it to
`tests/api/` (or split out the pure part).

### Service-level tests (the `db`-fixture exception)

A small number of files here are **service-level** rather than pure-logic: they call a service
function directly and assert against the result, using the shared root `db` fixture for state
(e.g. `test_plugin_sync_propagation.py`, `models/test_session_sender.py`). They have **no
TestClient and no HTTP**, so they are not API integration tests — but they are not pure no-I/O units
either. Because the root `db` fixture lazily connects to the test database, these files require the
migrated `app_test` schema; the unit `conftest.py` only no-ops migration *seeding*, not the database
itself. Keep such files rare and clearly documented in their module docstring. New pure-logic tests
should not depend on `db`.

## Cross-reference convention

When a private helper is unit-tested here but its API-observable behavior is also covered by a
scenario in `tests/api/`, leave a one-line pointer in both files so the pair stays discoverable.
See `tests/api/agents/commands/agents_cli_commands_test.py` (module docstring "Notes") paired with
`tests/unit/test_cli_commands_service.py` for the established pattern.

## Running

```bash
docker compose exec backend python -m pytest tests/unit/ -q
```

## Notable files

- `test_opencode_event_transformer.py` / `test_claude_code_event_transformer.py` — SDK event
  translation (text/reasoning buffering, tool events, permissions). The OpenCode adapter background
  on why these exist (per-mode server isolation, SSE timing, permission handling) is in the
  project docs: `docs/agents/agent_environment_core/multi_sdk.md` and `tools_approval_management.md`.
- `test_egress_guard.py` — SSRF egress allow/deny predicates and DNS-rebind resolution checks.
- `test_mcp_provider_oauth.py` — MCP provider OAuth helper internals (PKCE, state store, token
  application, lifecycle-state derivation). End-to-end OAuth/DCR flows are in
  `tests/api/mcp_integration/test_a2a_connector_oauth_dcr.py`.
- `test_mcp_resources_helpers.py`, `test_mcp_notifications.py`, `test_mcp_prompts.py` — MCP private
  helpers (URI parsing, MIME guessing, prompt-line parsing, notification building).
- `test_plugin_sync_propagation.py` — plugin-spec merge / propagation logic (service-level, no
  TestClient).
