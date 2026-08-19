# Backend Testing

## Running Tests

```bash
make test-backend
```

This executes `docker compose exec backend python -m pytest tests/ -v` inside the running backend container.

Prerequisites:
- Docker services must be running (`make up` or `docker compose up -d`)
- The `app_test` database must exist in the `db` container (created automatically on first `docker compose up` by `scripts/create-test-db.sh`)
- If the database was created before the init script was added, create it manually once:
  ```bash
  docker compose exec db psql -U postgres -c "CREATE DATABASE app_test;"
  ```

## Architecture

### API-Only Integration Tests

All tests interact with the backend **exclusively through HTTP requests** using FastAPI's `TestClient`. No test imports from `app.crud`, `app.services`, or `app.core.security` are allowed.

This means:
- **Setup**: Create users via `POST /users/signup`, create items via `POST /items/`, etc.
- **Verification**: Check API responses, verify side-effects by logging in with new credentials or fetching resources via API endpoints -- not by querying the database directly.
- **No direct DB access**: Tests do not import `Session`, `select`, or any ORM/CRUD functions.

By hitting only the API surface, each test implicitly covers:
1. Route registration and URL matching
2. Dependency injection (auth, DB sessions)
3. Request parsing and validation (Pydantic/SQLModel schemas)
4. Business logic in services and CRUD layers
5. Database queries and transactions
6. Response serialization and status codes
7. Authentication and authorization guards

### Separate Test Database

Tests run against a dedicated PostgreSQL database (`app_test`), not the application database (`app`). This is configured via environment variables passed to the backend container in `docker-compose.override.yml`:

| Variable | Value | Description |
|---|---|---|
| `TEST_DB_SERVER` | `db` | Hostname of the Postgres container |
| `TEST_DB_PORT` | `5432` | Port |
| `TEST_DB_NAME` | `app_test` | Test database name |
| `TEST_DB_USER` | `${POSTGRES_USER}` | Same user as the main DB |
| `TEST_DB_PASSWORD` | `${POSTGRES_PASSWORD}` | Same password as the main DB |

These are read by `app.core.config.Settings` and assembled into `TEST_SQLALCHEMY_DATABASE_URI`. The test engine in `conftest.py` connects to this URI. If `TEST_DB_SERVER` is not set, pytest fails immediately with a clear error.

### Automatic Migrations

Before any test runs, the session-scoped `setup_db` fixture in `conftest.py`:
1. Runs `alembic upgrade head` against the **test database** (not the app database)
2. Seeds the superuser via `init_db()`

This ensures the test database schema is always up to date with the latest migrations. Alembic's `env.py` respects a `sqlalchemy.url` set on the config object, which the fixture sets to the test DB URI before calling `command.upgrade`.

### Transaction Isolation (Savepoint Pattern)

Every test runs inside a database transaction that is **rolled back** after the test completes. This is implemented using the SQLAlchemy savepoint pattern:

1. `db` fixture opens a connection and begins an outer transaction
2. A nested savepoint is created inside that transaction
3. When app code calls `session.commit()`, it commits the savepoint (not the outer transaction)
4. An `after_transaction_end` event listener re-creates the savepoint after each commit
5. After the test, the outer transaction is rolled back, undoing **all** changes

This means:
- Every test starts with a clean slate (only the seeded superuser exists)
- Tests never affect each other, regardless of execution order
- No manual cleanup is needed
- The `client` fixture overrides FastAPI's `get_db` dependency to inject the test session

## Directory Structure

```
tests/
  conftest.py              # Root fixtures: db, client, auth headers (full app lifespan, test DB)
  api/                     # API-only integration tests (TestClient), one dir per route domain.
    a2a_integration/       #   See "Rules" — these tests hit HTTP only, never app internals.
    agent_environments/
    agentic_teams/
    agents/                # conftest.py: env stubs, background task collector, create_session patches.
                           #   Largest domain (84 files / 610 tests) — split into topic groups.
                           #   Subdirs inherit agents/conftest.py; see agents/README.md for the map.
      bundles/             #   publish: revisions, snapshots, credential specs, template sharing
      bundles_install/     #   install/update: credential resolution, readiness gate, propagation
      agent_api/           #   Agent REST API: proxy, policy, caller scopes, external keys
      git/                 #   git-backed versioning: checkout/pull/push, conflicts, recovery
      improvement_requests/#   improvement requests: targeting, lifecycle, archive, rate limits
      schedules/           #   schedule CRUD, types + logs, manual run
      webapp/              #   webapp shares, serving, webapp chat, interface config
      sessions/            #   session lifecycle + streaming, message attachments
      commands/            #   slash/CLI commands, /files, /run, agent status
      guest_shares/        #   guest share links: CRUD, auth, security code, sessions
      integrations/        #   email, webhooks, capability flags, router trigger
      core/                #   create-flow, limits, prompt sync, plugins, tokens, delegation
    ai_credentials/        # conftest.py: env stubs for credential propagation tests
    app_auth/
    app_data/
    app_mcp/
    app_sync/
    auth/                  # test_login.py, test_users.py — login, signup, password mgmt
    cli/
    credentials/           # conftest.py: heavy env/background stubs (scoped to files that need them)
    dashboards/
    desktop_auth/
    external/
    identity/
    input_tasks/
    knowledge_sources/
    mcp_integration/       # conftest.py: MCP OAuth + tool-handler create_session patches
    notifications/
    security_events/
    ssh_keys/
    users/                 # conftest.py: env stubs + MFA rate-limit bucket reset
    workspaces/
    (items/ is empty — the items feature/test was removed; see "Deferred" in the refactor plan)
  unit/                    # Pure unit tests — see "Unit Tests" below. No DB, no TestClient.
    conftest.py            # No-op setup_db override + env-template sys.path
    test_*.py
  architecture/            # Contract / drift tests — see "Architecture Tests" below.
  stubs/                   # Test doubles for external services
    environment_adapter_stub.py
    email_stubs.py
    agent_env_stub.py
    socketio_stub.py
  utils/
    utils.py               # random_lower_string(), random_email(), get_superuser_token_headers()
    user.py                # create_random_user(), user_authentication_headers()
    agent.py               # create_agent_via_api(), get_agent(), enable_a2a(), configure/enable_email_integration()
    ai_credential.py       # create_random_ai_credential(), set/update/delete/get helpers
    a2a.py                 # setup_a2a_agent(), a2a_headers(), extract_parts_from_sse_event(), extract_task_id(), etc.
    background_tasks.py    # drain_tasks() for deferred background task execution
    bundle.py              # publish_bundle(), install_bundle(), make_bundle_public(), bundle credential helpers
    credential.py          # create_random_credential(), share_credential_via_api(), set_credential_sharing(), get_credential()
    desktop_auth.py        # obtain_desktop_tokens() — full authorize/consent/exchange dance
    environment.py         # set_environment_status(), link_ai_credential_to_environment() (documented DB-seam helpers)
    fixtures.py            # shared stub fixtures, CREATE_SESSION_TARGETS_*, BACKGROUND_TASK_TARGETS_* patch lists
    mail_server.py         # create_imap_server(), create_smtp_server(), process_emails_with_stub()
    platform_token.py      # mint_platform_token() — raw/expired/scoped JWTs (documented app.core.security exemption)
    session.py             # get_agent_session(), get_session(), list_sessions()
    message.py             # get_messages_by_role(), list_messages()
    knowledge_source.py    # create/get/list/update/delete/enable/disable_knowledge_source()
```

## Unit Tests (`tests/unit/`)

The API-only rule (Rule 1 below) applies to **`tests/api/` only**. `tests/unit/` is the home for
pure unit tests, and the rules there are different:

- **Allowed here**: importing from `app.services`, `app.core`, adapters, and env-template modules
  directly, and calling functions/classes in isolation. There is **no `client` and no `db`** — the
  unit conftest (`tests/unit/conftest.py`) overrides `setup_db` to a no-op, so no Postgres connection
  is opened.
- **What belongs in `tests/unit/`**: pure logic with no I/O — event transformers, parsers, decision
  tables, similarity/scoring functions, URL/path helpers, private `_helper` functions, egress-guard
  predicates, MagicMock-driven defensive-branch tests, and anything that asserts module-level
  constants. If a test only needs a function and some plain Python inputs, it goes here.
- **What does NOT belong here**: anything that needs the database, a real HTTP round-trip, the full
  app lifespan, or background-task draining. Those are integration tests and live in `tests/api/`.
  Litmus test: **if a "unit" test secretly needs `db` or `client`, it is not a pure unit test** —
  move it back to `tests/api/` (or split it). New unit files must run green under
  `tests/unit/conftest.py` with no DB engine.

### Cross-reference convention

When a private helper is unit-tested in `tests/unit/` but its API-observable behavior is covered by a
scenario in `tests/api/`, leave a one-line pointer in **both** directions so the pair stays
discoverable:

- In the api test file (module docstring or a comment near the related scenario):
  `# Unit tests for parse_commands_file live in tests/unit/test_cli_commands_service.py`
- In the unit test file: a docstring note that the end-to-end / API-observable path is covered in the
  corresponding `tests/api/<domain>/..._test.py`.

See `tests/api/agents/commands/agents_cli_commands_test.py` (module docstring "Notes") for the established
pattern.

```bash
docker compose exec backend python -m pytest tests/unit/ -q
```

## Architecture Tests (`tests/architecture/`)

Contract / drift tests that assert structural invariants about the **application source tree** rather
than runtime behavior. They scan `backend/app/` and fail when the codebase drifts away from an
assumption the test suite relies on. Examples:

- `patch_target_drift_test.py` — every `app` module that imports `create_session` /
  `create_task_with_error_logging` must appear in the corresponding patch-target list in
  `tests/utils/fixtures.py` (or an explicit, commented allowlist). Otherwise a service silently opens
  sessions on the real engine during tests.
- `channel_ingestion_callers_test.py` — guards the set of modules participating in channel ingestion.

These tests import and inspect `app` modules directly; the API-only rule does not apply to them.

## Writing New Tests

### File Placement

Place test files under `tests/api/<domain>/test_<domain>.py`, mirroring the route structure in `app/api/routes/`. Create an `__init__.py` in each new directory.

A domain that grows past ~20 files is split into **topic group** subpackages one level deeper (`tests/api/<domain>/<group>/`). Group subdirs inherit the domain's `conftest.py` automatically, so they need only an `__init__.py`. `tests/api/agents/` is the one domain split this way today — its `README.md` carries the group map and the placement rule. When writing into a split domain, put the file in the matching group, never at the domain root.

Some domains have their own `README.md` with domain-specific testing patterns (e.g., stubs, extra fixtures, relaxed rules). **Always check for a `README.md` in the target directory before writing tests** — for example, `tests/api/agents/README.md` documents the session mocking and environment stubs required for agent tests.

### Fixtures

Every test function receives fixtures via pytest dependency injection. The key fixtures defined in `conftest.py`:

| Fixture | Scope | Description |
|---|---|---|
| `client` | function | `TestClient` with the test DB session injected |
| `superuser_token_headers` | function | `{"Authorization": "Bearer <token>"}` for the superuser |
| `normal_user_token_headers` | function | Auth headers for `test@example.com` (created if needed) |

Use `client` in every test. Use the auth header fixtures when the endpoint requires authentication.

### Test Structure: Scenario-Based Tests

Prefer **scenario-based tests** that walk through a user story end-to-end rather than writing many small atomic tests for individual operations. A single test function should set up state, perform a sequence of related actions, and verify the outcome at each step.

**Why scenarios over atomic tests:**
- They catch integration issues between steps (e.g., create → list → update → verify update appears in list)
- They mirror real user workflows, so failures point to actual broken behavior
- They reduce test setup duplication — each phase builds on the previous one
- Fewer tests to maintain while covering more surface area

**How to structure a scenario test:**
- Use comment headers (`# ── Phase N: ...`) to separate logical steps
- Assert at each phase, not just at the end — this makes failures easy to locate
- The docstring should outline the full story as a numbered list

```python
def test_widget_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full CRUD lifecycle:
      1. Create widget
      2. List widgets → verify it appears
      3. Update widget
      4. Verify update persisted
      5. Delete widget
      6. Verify it's gone
    """
    # ── Phase 1: Create ───────────────────────────────────────────────
    created = create_widget(client, superuser_token_headers, name="My Widget")
    widget_id = created["id"]

    # ── Phase 2: List → widget is present ─────────────────────────────
    widgets = list_widgets(client, superuser_token_headers)
    assert any(w["id"] == widget_id for w in widgets)

    # ── Phase 3: Update ───────────────────────────────────────────────
    updated = update_widget(client, superuser_token_headers, widget_id, name="Renamed")
    assert updated["name"] == "Renamed"

    # ── Phase 4: Verify update persisted ──────────────────────────────
    fetched = get_widget(client, superuser_token_headers, widget_id)
    assert fetched["name"] == "Renamed"

    # ── Phase 5: Delete ───────────────────────────────────────────────
    delete_widget(client, superuser_token_headers, widget_id)

    # ── Phase 6: Verify gone ──────────────────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/widgets/{widget_id}",
                   headers=superuser_token_headers)
    assert r.status_code == 404
```

**Fold error and auth checks into the scenario as phases, not separate tests.**

404 (not-found), ownership guards, and auth rejections are not standalone stories — they are
observable properties of the resource you just created. Test them inline, right after the
resource exists, so no extra setup is needed:

```python
    # ── Phase N: Auth and ownership guards ────────────────────────────
    # Unauthenticated request is rejected
    assert client.get(f"{_BASE}/").status_code in (401, 403)

    # Other user cannot read or mutate the resource
    other = create_random_user(client)
    other_h = user_authentication_headers(client=client, email=other["email"], password=other["_password"])
    assert client.get(f"{_BASE}/{resource_id}", headers=other_h).status_code == 404
    assert client.put(f"{_BASE}/{resource_id}", headers=other_h, json={}).status_code == 404
    assert client.delete(f"{_BASE}/{resource_id}", headers=other_h).status_code == 404

    # Non-existent ID returns 404
    ghost = str(uuid.uuid4())
    assert client.get(f"{_BASE}/{ghost}", headers=headers).status_code == 404
```

**A standalone test is only justified when:**
- The error case requires completely different setup (e.g., a separate user role or a
  precondition that cannot exist in the main flow)
- Testing a validation rule that fires before any resource is created (e.g., a missing
  required field on POST)

### Creating Test Data

Always create test data through API endpoints, never through direct DB calls. Reusable helpers live in `tests/utils/`:

```python
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.agent import create_agent_via_api
```

### Test Utility Helpers

Every repeated API call pattern should be extracted into a helper in `tests/utils/<domain>.py`. Helpers follow these conventions:

1. **Encapsulate HTTP call + status assertion**, return parsed JSON:
   ```python
   def set_ai_credential_default(client, token_headers, credential_id) -> dict:
       r = client.post(f"{settings.API_V1_STR}/ai-credentials/{credential_id}/set-default",
                       headers=token_headers)
       assert r.status_code == 200
       return r.json()
   ```

2. **Compose common sequences** via parameters instead of separate calls:
   ```python
   # Instead of create + set_default in every test:
   cred = create_random_ai_credential(client, headers, set_default=True)
   ```

3. **Keep inline calls only when testing the endpoint itself** (checking specific status codes, error responses, or response structure):
   ```python
   # Testing 403 — keep inline, don't use the helper
   r = client.post(f".../{cred['id']}/set-default", headers=other_user_headers)
   assert r.status_code == 403
   ```

4. **Naming**: `create_*` for POST, `get_*` for GET, `update_*` for PATCH, `delete_*` for DELETE, with the domain as prefix (e.g., `create_random_ai_credential`, `get_ai_credentials_profile`).

### Verifying Side-Effects

Instead of querying the database directly, verify through the API:

```python
# BAD - direct DB access
user = crud.get_user_by_email(session=db, email=email)
assert verify_password(new_password, user.hashed_password)

# GOOD - verify via API
r = client.post(f"{settings.API_V1_STR}/login/access-token",
                data={"username": email, "password": new_password})
assert r.status_code == 200
```

### Mocking External Services

Use `unittest.mock.patch` for external services (email, OAuth, etc.):

```python
from unittest.mock import patch

def test_password_recovery(client: TestClient) -> None:
    with (
        patch("app.core.config.settings.SMTP_HOST", "smtp.example.com"),
        patch("app.core.config.settings.SMTP_USER", "admin@example.com"),
    ):
        r = client.post(f"{settings.API_V1_STR}/password-recovery/{email}")
        assert r.status_code == 200
```

## Rules

1. **No imports from `app.crud`, `app.services`, or `app.core.security`** in `tests/api/` files. The only allowed app imports are `app.core.config.settings` (for API URL prefix and config values) and `app.utils` (for token generation in password-reset tests). This rule applies to `tests/api/` only — `tests/unit/` and `tests/architecture/` may import app internals (see "Unit Tests" and "Architecture Tests" above).
2. **All test data created via API endpoints.** Use the helpers in `tests/utils/`.
3. **All verification via API responses.** Check status codes and JSON bodies. Verify side-effects by calling other endpoints (e.g., log in to verify a password change).
4. **Each test is independent.** Transaction rollback ensures no state leaks. Do not rely on test execution order.
5. **Use random data.** Use `random_email()` and `random_lower_string()` for test data to avoid collisions.
6. **Mock external calls.** Patch SMTP, OAuth, and any external HTTP calls.
7. **Extract repeated API calls into `tests/utils/` helpers.** If the same endpoint call appears in multiple tests as setup (not as the thing being tested), wrap it in a utility function. Compose common multi-step sequences via parameters (e.g., `set_default=True`).

## Testing Session-Driven Flows (Tasks, Agents, Streaming)

Tests that exercise agent streaming (message send → agent response → session completion) require understanding the async architecture. The key mental model:

### Execution Timing

`execute_task()` and `send_message()` **return immediately** — they only schedule `process_pending_messages` as a background task. The actual streaming (agent-env connector call, event emission, session status updates) happens inside `drain_tasks()`. This means:

```python
# WRONG — stub not active during streaming
with patch("app.services.message_service.agent_env_connector", stub):
    exec_result = execute_task(client, headers, task_id)
# drain_tasks() runs outside the patch — stub not used!
drain_tasks()

# CORRECT — stub active during drain
with patch("app.services.message_service.agent_env_connector", stub):
    exec_result = execute_task(client, headers, task_id)
    drain_tasks()  # streaming happens here, inside the patch
```

If you need the session_id before building the stub (e.g., to include `source_session_id` in a tool call), call `execute_task` first with a placeholder stub, then build the real stub and drain:

```python
with patch("...", StubAgentEnvConnector(response_text="placeholder")):
    exec_result = execute_task(client, headers, task_id)

session_id = str(exec_result["session_id"])
real_stub = ScriptedAgentEnvConnector(client=client, auth_headers=headers, steps=[...])
with patch("...", real_stub):
    drain_tasks()
```

### Session Completion Drives Task Status

After `drain_tasks()`, session completion event handlers fire and automatically sync task status via `sync_task_status_from_sessions`. **Do not manually call `agent_update_status("completed")`** — verify the automatic transition instead:

```python
with patch("...", stub):
    execute_task(client, headers, task_id)
    drain_tasks()

# Task auto-completed by session lifecycle events
task = get_task(client, headers, task_id)
assert task["status"] == "completed"  # session-driven, not manual
```

A task with subtasks stays `in_progress` until ALL subtasks complete. When a subtask completes, feedback delivery sends a message to the parent's session, which re-streams and re-syncs the parent.

### Cascading Drain Rounds

`drain_tasks()` runs up to 10 rounds. Each round may spawn new tasks:

1. `process_pending_messages` → stream → "done" → emits STREAM_COMPLETED
2. Event handlers fire (SessionService, InputTaskService, ActivityService)
3. `deliver_feedback_to_source` may schedule another `process_pending_messages`
4. The feedback stream completes → another round of handlers

This is correct behavior. Stubs must handle being called multiple times — `ScriptedAgentEnvConnector` runs scripted steps only on the first call and falls back to a simple response on subsequent calls.

### Stub Selection

| Scenario | Stub | Why |
|----------|------|-----|
| Simple agent response | `StubAgentEnvConnector(response_text="...")` | Just needs to yield events and complete |
| Agent calls MCP tools mid-stream | `ScriptedAgentEnvConnector(client, headers, steps=[...])` | Makes real HTTP calls to backend during stream |
| Error response | `StubAgentEnvConnector(events=[{"type": "error", ...}])` | Custom event sequence |

### ScriptedAgentEnvConnector — Simulating MCP Tool Calls

In production, the agent SDK calls MCP tools (HTTP requests back to the backend) **during** the SSE stream, before the "done" event. `ScriptedAgentEnvConnector` replicates this:

```python
stub = ScriptedAgentEnvConnector(
    client=client,
    auth_headers=headers,
    steps=[
        {"type": "assistant", "content": "I'll create a subtask."},
        {
            "type": "tool_call",
            "endpoint": f"/api/v1/agent/tasks/{task_id}/subtask",
            "method": "POST",
            "json": {"title": "Do X", "assigned_to": "Worker Agent"},
            "tool_name": "mcp__agent_task__create_subtask",
        },
        {"type": "assistant", "content": "Subtask created."},
    ],
)
```

The `tool_call` step makes a real HTTP request to the TestClient. Results are tracked in `stub.tool_results`. Only the first `stream_chat` call executes scripted steps; subsequent calls (from cascading feedback) use a simple fallback (`stub.fallback_call_count` tracks how many times).

### Source Code Invariants That Tests Depend On

Tests rely on these patterns in the application code. If you find code that violates them, **fix the source code rather than working around it in the test**:

1. **Event handlers must use `create_session()`** (not `DBSession(engine)`). Handlers using `DBSession(engine)` create sessions outside the test transaction — they can't see test data and silently return. Every handler in `session_service.py`, `activity_service.py`, and `input_task_service.py` should use `with create_session() as db:`.

2. **Status transitions must go through `update_task_status()`** for audit trail. If you find code setting `task.status = ...` directly, it bypasses `TaskStatusHistory` and system comments. Fix the source to use `update_task_status()`.

3. **New services importing `create_session` must be added to patch targets.** If a new service file imports `from app.core.db import create_session`, add `"app.services.new_service.create_session"` to `CREATE_SESSION_TARGETS_AGENT` in `tests/utils/fixtures.py`. Similarly for `create_task_with_error_logging` → `BACKGROUND_TASK_TARGETS_FULL`.

When a test needs a workaround (e.g., explicit status override, relaxed assertions), that's a signal to investigate whether the source code has a pattern violation.

## Code Style (for application code)

- **Datetime**: Use `datetime.now(datetime.UTC)` instead of deprecated `datetime.utcnow()`.
