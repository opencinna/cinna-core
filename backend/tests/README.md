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
  utils/
    utils.py               # random_lower_string(), random_email(), get_superuser_token_headers()
    user.py                # create_random_user(), user_authentication_headers(), authentication_token_from_email()
    item.py                # create_random_item()
```

## Writing New Tests

### File Placement

Place test files under `tests/api/<domain>/test_<domain>.py`, mirroring the route structure in `app/api/routes/`. Create an `__init__.py` in each new directory.

### Fixtures

Every test function receives fixtures via pytest dependency injection. The key fixtures defined in `conftest.py`:

| Fixture | Scope | Description |
|---|---|---|
| `client` | function | `TestClient` with the test DB session injected |
| `superuser_token_headers` | function | `{"Authorization": "Bearer <token>"}` for the superuser |
| `normal_user_token_headers` | function | Auth headers for `test@example.com` (created if needed) |

Use `client` in every test. Use the auth header fixtures when the endpoint requires authentication.

### Test Structure

```python
from fastapi.testclient import TestClient
from app.core.config import settings
from tests.utils.utils import random_email, random_lower_string


def test_create_widget(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    data = {"name": "My Widget", "value": 42}
    r = client.post(
        f"{settings.API_V1_STR}/widgets/",
        headers=superuser_token_headers,
        json=data,
    )
    assert r.status_code == 200
    created = r.json()
    assert created["name"] == "My Widget"
    assert "id" in created
```

### Creating Test Data

Always create test data through API endpoints, never through direct DB calls:

```python
# Create a user
def _signup_user(client, email=None, password=None):
    email = email or random_email()
    password = password or random_lower_string()
    r = client.post(f"{settings.API_V1_STR}/users/signup",
                    json={"email": email, "password": password})
    assert r.status_code == 200
    result = r.json()
    result["_password"] = password  # stash for later use
    return result

# Log in and get auth headers
def _login(client, email, password):
    r = client.post(f"{settings.API_V1_STR}/login/access-token",
                    data={"username": email, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
```

Reusable helpers live in `tests/utils/`. Use `create_random_user(client)` and `create_random_item(client, token_headers)` from there.

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

## Code Style (for application code)

- **Datetime**: Use `datetime.now(datetime.UTC)` instead of deprecated `datetime.utcnow()`.
