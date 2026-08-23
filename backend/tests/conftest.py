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
    # join_transaction_mode="create_savepoint" is SQLAlchemy 2.0's documented
    # mode for exactly this pattern (a Session joining an externally-managed
    # connection/transaction via SAVEPOINTs). Without it, a caught
    # IntegrityError followed by session.rollback() (the standard "unique
    # constraint race -> catch -> rollback -> re-read" idiom used by several
    # services, e.g. ServerConfigService.get_or_create and
    # ChannelInboundService._upsert_binding) rolls back past the current
    # SAVEPOINT and expires/detaches objects committed earlier in the SAME
    # test — a bare `session.get()` on them then raises ObjectDeletedError,
    # even though their rows are still present in the real transaction.
    #
    # Why this wasn't already covered by the default: SQLAlchemy's default
    # join mode, "conditional_savepoint", picks "create_savepoint" only when
    # the Session is joining a SAVEPOINT-capable *nested* transaction; here we
    # hand it a plain `connection.begin()` (an outer, non-nested transaction),
    # so the condition can never be satisfied and it silently falls back to
    # "rollback_only" — the mode that produced the bug. A future refactor of
    # this fixture that swaps `connection.begin()` for `connection.
    # begin_nested()` (or otherwise changes what's passed to `bind=`) would
    # put this back in "conditional_savepoint" territory and needs the same
    # scrutiny.
    #
    # The `session.begin_nested()` + `after_transaction_end` listener below is
    # now redundant with `create_savepoint` — SQLAlchemy 2.0's documented
    # recipe for this pattern is the constructor parameter alone, which
    # already manages the SAVEPOINT lifecycle across commits. Left in place
    # rather than removed: two production methods
    # (`GitSourceService._clear_poisoned_transaction` /
    # `_cleanup_orphan_bundle`) were written against the old (buggy) fixture
    # behavior and currently branch on `session.get_nested_transaction()`
    # being non-None, which the listener is what keeps true. Removing the
    # listener is a separate, tracked follow-up that needs its own review
    # alongside those two methods, not a side effect of a comment edit.
    #
    # Regression scope actually run for this change (not "everything"):
    # tests/api/{auth,credentials,identity,app_mcp}/, tests/api/agents/{core,
    # sessions,git,bundles,bundles_install}/, tests/unit/, tests/architecture/,
    # and tests/api/server_channels/ (the domain that found the bug — see
    # server_channels_security_invariants_test.py::
    # test_lost_race_ingest_branch_declines_the_loser, whose two-webhook-race
    # setup is what first hit it). All green, including a case the reviewer
    # flagged as at-risk (a compensating-delete path in
    # agents_bundles_install_context_test.py that had never actually executed
    # under the old, buggy rollback behavior). Domains with the heaviest
    # `session.rollback()`-after-`IntegrityError` usage
    # (`git_source_service.py`, `install_service.py`) were deliberately
    # included, not assumed safe by analogy to lighter-usage domains.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    # Start a nested savepoint (redundant with join_transaction_mode above;
    # see the comment on the Session(...) construction for why it stays).
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
