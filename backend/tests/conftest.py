from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, create_engine
from sqlmodel import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.db import init_db
from app.main import app
from tests.utils.user import authentication_token_from_email
from tests.utils.utils import get_superuser_token_headers


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


@pytest.fixture(scope="function")
def superuser_token_headers(client: TestClient) -> dict[str, str]:
    return get_superuser_token_headers(client)


@pytest.fixture(scope="function")
def normal_user_token_headers(client: TestClient) -> dict[str, str]:
    return authentication_token_from_email(
        client=client, email=settings.EMAIL_TEST_USER
    )
