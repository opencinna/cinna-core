from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from app.core.config import settings
from app.models import AccountOrigin, User, UserCreate
from app.services.users.user_service import UserService

engine = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))


def create_session():
    """Create a new database session. Patchable in tests."""
    return Session(engine)


# ── Single-leader scheduler sessions ────────────────────────────────────────


@contextmanager
def leader_session(lock_key: int) -> Iterator[Session | None]:
    """Yield a session holding a Postgres advisory lock, or ``None`` to skip.

    ``None`` means another process already holds ``lock_key`` and this run is
    not the leader. Under N gunicorn/uvicorn workers each runs its own
    ``BackgroundScheduler``, so without this guard every worker would run the
    same batch.

    **The lock has to live on the same physical connection for its whole life.**
    ``pg_try_advisory_lock`` is *connection*-scoped, while a ``Session`` bound to
    an **engine** hands its connection back to the pool at every ``commit()``.
    A sweep that commits per row would therefore strand the lock on a pooled
    connection: the matching ``pg_advisory_unlock`` runs on whatever connection
    the pool hands back next, returns false, and the lock is never released —
    so every subsequent run is locked out forever and the feature silently
    disables itself after its first productive run. Binding the ``Session`` to
    an explicit ``engine.connect()`` pins it.

    This is the ONLY implementation of that pattern. It was previously copied
    into three schedulers; two copies were correct, the third took the lock on a
    pooled session and leaked it exactly as described (a live bug), and each copy
    carried its own restatement of the reasoning above.

    Deliberately silent on the not-leader path: every caller already logs its
    own skip, with the context and at the level it wants (one of them names the
    bundle it was converging; another uses ``debug`` because it ticks every two
    minutes). A line here as well would double every one of them.

    Args:
        lock_key: A stable, arbitrary 64-bit key. Callers own their key and must
            not share one unless they genuinely must not run concurrently.
    """
    if settings.TESTING:
        # Under test there is no cross-process concurrency to guard against, and
        # the harness patches ``create_session`` to hand back the rolled-back
        # test transaction. Checking out a real pooled connection here would
        # escape that isolation and write to the live database.
        with create_session() as session:
            yield session
        return

    with engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}
        ).scalar_one()
        connection.commit()
        if not acquired:
            yield None
            return
        try:
            with Session(bind=connection) as session:
                yield session
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key}
            )
            connection.commit()


# make sure all SQLModel models are imported (app.models) before initializing DB
# otherwise, SQLModel might fail to initialize relationships properly
# for more details: https://github.com/fastapi/full-stack-fastapi-template/issues/28


def init_db(session: Session) -> None:
    # Tables should be created with Alembic migrations
    # But if you don't want to use migrations, create
    # the tables un-commenting the next lines
    # from sqlmodel import SQLModel

    # This works because the models are already imported and registered from app.models
    # SQLModel.metadata.create_all(engine)

    user = session.exec(
        select(User).where(User.email == settings.FIRST_SUPERUSER)
    ).first()
    if not user:
        user_in = UserCreate(
            email=settings.FIRST_SUPERUSER,
            password=settings.FIRST_SUPERUSER_PASSWORD,
            is_superuser=True,
        )
        # ``seed`` is an ungated origin: the first superuser has to be
        # creatable on an instance whose access policy does not exist yet.
        user = UserService.create_user(
            session=session, user_create=user_in, origin=AccountOrigin.SEED
        )
    else:
        # Phase 3 — enforce the ``role ⇔ is_superuser`` invariant on the
        # bootstrapped superuser.  This catches DBs seeded before the
        # ``user.role`` column existed (Phase 3 migration backfilled
        # historical rows; this re-asserts the rule on first-boot).
        from app.models.users.user import UserRole
        if user.is_superuser and user.role != UserRole.ADMIN.value:
            user.role = UserRole.ADMIN.value
            session.add(user)
            session.commit()

    # Materialize the ServerConfig singleton here, at prestart, rather than
    # leaving it to whichever request happens to read the access policy
    # first. ``get_or_create`` COMMITS when it creates the row, and the
    # policy is now read from login, signup, the Google callback and every
    # ``UserPublic`` projection — none of which should be the thing that
    # commits an unrelated row. Creating it once, here, means every later
    # call is a pure read. Also runs the env seed on a fresh instance.
    from app.services.server_config.server_config_service import (
        ServerConfigService,
    )

    ServerConfigService.get_or_create(session)
