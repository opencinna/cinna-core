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
  conftest.py              # Fixtures: db, client, auth headers
  api/
    auth/
      test_login.py        # Login, password recovery, password reset
      test_users.py        # User CRUD, signup, password management
    items/
      test_items.py        # Item CRUD
    agents/
      conftest.py          # Environment stubs, background task collector
      agents_email_integration_test.py
    ai_credentials/
      conftest.py          # Environment stubs for credential propagation tests
      test_ai_credentials.py
      test_ai_credentials_propagation.py
    knowledge_sources/
      test_knowledge_sources.py  # Source lifecycle, check-access/refresh, discoverable flow
  stubs/                   # Test doubles for external services
    environment_adapter_stub.py
    email_stubs.py
    agent_env_stub.py
    socketio_stub.py
  utils/
    utils.py               # random_lower_string(), random_email(), get_superuser_token_headers()
    user.py                # create_random_user(), user_authentication_headers()
    item.py                # create_random_item()
    agent.py               # create_agent_via_api(), get_agent(), enable_a2a(), configure/enable_email_integration()
    ai_credential.py       # create_random_ai_credential(), set/update/delete/get helpers
    a2a.py                 # setup_a2a_agent(), send_a2a_streaming_message(), create_access_token(), etc.
    background_tasks.py    # drain_tasks() for deferred background task execution
    mail_server.py         # create_imap_server(), create_smtp_server(), process_emails_with_stub()
    session.py             # get_agent_session(), list_sessions()
    message.py             # get_messages_by_role(), list_messages()
    knowledge_source.py    # create/get/list/update/delete/enable/disable_knowledge_source()
```

## Writing New Tests

### File Placement

Place test files under `tests/api/<domain>/test_<domain>.py`, mirroring the route structure in `app/api/routes/`. Create an `__init__.py` in each new directory.

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

1. **No imports from `app.crud`, `app.services`, or `app.core.security`** in test files. The only allowed app imports are `app.core.config.settings` (for API URL prefix and config values) and `app.utils` (for token generation in password-reset tests).
2. **All test data created via API endpoints.** Use the helpers in `tests/utils/`.
3. **All verification via API responses.** Check status codes and JSON bodies. Verify side-effects by calling other endpoints (e.g., log in to verify a password change).
4. **Each test is independent.** Transaction rollback ensures no state leaks. Do not rely on test execution order.
5. **Use random data.** Use `random_email()` and `random_lower_string()` for test data to avoid collisions.
6. **Mock external calls.** Patch SMTP, OAuth, and any external HTTP calls.
7. **Extract repeated API calls into `tests/utils/` helpers.** If the same endpoint call appears in multiple tests as setup (not as the thing being tested), wrap it in a utility function. Compose common multi-step sequences via parameters (e.g., `set_default=True`).

## Code Style (for application code)

- **Datetime**: Use `datetime.now(datetime.UTC)` instead of deprecated `datetime.utcnow()`.
