from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, create_engine
from sqlmodel import Session

from app.core.config import settings

# Enable test mode BEFORE importing the app. ``app.main.lifespan`` reads this
# flag to skip starting background schedulers (APScheduler jobs bind to the
# application DB engine and would mutate the dev DB from inside a test — an
# isolation escape). Setting it here guarantees it is True at the time the
# lifespan runs (when the per-test ``TestClient(app)`` context is entered).
settings.TESTING = True

from app.api.deps import get_db  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from tests.utils.user import authentication_token_from_email  # noqa: E402
from tests.utils.utils import get_superuser_token_headers  # noqa: E402


def _get_test_engine():
    """Build a SQLAlchemy engine pointing at the test database."""
    uri = settings.TEST_SQLALCHEMY_DATABASE_URI
    if not uri:
        raise RuntimeError(
            "TEST_DB_SERVER is not set. "
            "Configure TEST_DB_* environment variables to run tests."
        )
    return create_engine(str(uri))


# Lazy engine — only created when a DB-dependent fixture is first used.
# This allows unit tests (tests/unit/) to run without a database connection.
_test_engine = None


def _ensure_test_engine():
    global _test_engine
    if _test_engine is None:
        _test_engine = _get_test_engine()
    return _test_engine


@pytest.fixture(scope="session", autouse=True)
def setup_db() -> Generator[None, None, None]:
    """Run Alembic migrations and seed the test database once per session."""
    engine = _ensure_test_engine()

    # Run Alembic migrations against the test DB
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", str(settings.TEST_SQLALCHEMY_DATABASE_URI))
    command.upgrade(alembic_cfg, "head")

    # Seed the superuser
    with Session(engine) as session:
        init_db(session)
    yield


@pytest.fixture(scope="function")
def db() -> Generator[Session, None, None]:
    """
    Per-test database session with transaction isolation.

    Opens a connection, begins a transaction, then creates a nested savepoint.
    The app code can call session.commit() freely — we intercept those commits
    and re-open a savepoint so the outer transaction is never committed.
    After the test, we roll back the outer transaction, undoing all changes.
    """
    engine = _ensure_test_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    # Start a nested savepoint
    session.begin_nested()

    # After each commit (which releases the savepoint), start a new one
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, transaction_record):
        if transaction_record.nested and not transaction_record._parent.nested:
            session.begin_nested()

    yield session

    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture(scope="function")
def client(db: Session) -> Generator[TestClient, None, None]:
    """
    Test client that uses the per-test database session.
    """

    def _get_test_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _get_test_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="session")
def superuser_token_headers(setup_db: None) -> dict[str, str]:
    """Auth headers for the seeded superuser, minted once per session.

    JWTs are stateless and the superuser is seeded by ``setup_db`` (committed to
    the test DB outside any rollback transaction), so its login token is stable
    for the whole session. Minting it once avoids a login round-trip on every
    test. We use a short-lived ``TestClient`` whose ``get_db`` yields a plain
    session on the test engine (not the per-test savepoint session) so this
    fixture has no dependency on the function-scoped ``db``/``client`` fixtures.
    """
    engine = _ensure_test_engine()

    def _get_session_db() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _get_session_db
    try:
        with TestClient(app) as c:
            return get_superuser_token_headers(c)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="function")
def normal_user_token_headers(client: TestClient) -> dict[str, str]:
    return authentication_token_from_email(
        client=client, email=settings.EMAIL_TEST_USER
    )
