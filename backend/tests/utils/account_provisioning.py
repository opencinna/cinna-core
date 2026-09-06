"""Reproducing database-level failure during account provisioning.

WHY THIS FILE HOLDS A RULE-1 EXEMPTION
--------------------------------------
``backend/tests/README.md`` Rule 1 keeps ``tests/api/`` off ``app.services``.
This module is the single, named place that import lives for the
account-provisioning invariant, in the same spirit as
``tests/utils/platform_token.py`` (``app.core.security``),
``tests/utils/environment.py`` (documented DB seams) and
``tests/utils/server_channel.py``'s ``flush_pending_bindings`` /
``replay_stream_completed`` — production events with **no HTTP surface that
can produce them**.

The invariant under test is *"account creation never fails because
provisioning failed"*, and the developer who implemented phase 2 reported it
broken three separate ways during development, none of which 1877 existing
tests or a happy-path smoke script caught. All three shared one property: they
only appear when the **Postgres transaction is aborted**, not when Python
merely raises. An ordinary ``ValueError`` injected into the path exercises the
``try``/``except`` and proves nothing, because the interesting failures are the
ones where the *recovery code itself* — a ``logger.warning`` whose argument is
a lazily-loaded ORM attribute, an audit ``session.commit()``, the caller's next
commit — is the thing that throws.

There is no HTTP request that aborts a transaction on purpose, so the honest
reproduction is to make a real SQL statement fail at the moment the production
code runs it. That is what :func:`failing_sql_statement` does, and there is no
API-level equivalent.

WHAT IS AND IS NOT EXEMPTED
---------------------------
Only two app imports: ``AccountProvisioningService`` (to hand it a session
directly, which no route does) and ``User`` (to look one up so it can be
handed over). Everything else these tests need — creating accounts, creating
managed credentials, reading back what was provisioned — still goes through
the API, from the test files.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlmodel import Session

from app.models.users.user import AccountOrigin, User
from app.services.users.account_provisioning_service import (
    AccountProvisioningService,
)

# Re-exported so test files can name an origin without importing app models
# themselves — the exemption stays in this one module.
__all__ = [
    "AccountOrigin",
    "BROKEN_SQL",
    "CHILD_CREDENTIAL_INSERT",
    "MEMBERSHIP_LOOKUP",
    "abort_transaction",
    "create_account_via_service",
    "failing_sql_statement",
    "get_user_row",
    "provision_account",
    "session_is_usable",
]

# A relation that cannot exist. Referencing it is a genuine statement-level
# failure: Postgres answers ``UndefinedTable`` *and* puts the surrounding
# transaction into the aborted state, where every later statement fails with
# ``InFailedSqlTransaction`` until something rolls back. That second half is
# the whole point — a Python exception alone would not reproduce the bugs this
# file exists to catch.
BROKEN_SQL = "SELECT 1 FROM cinna_no_such_table_used_only_by_tests"

# Statement fragments worth injecting against, named so test files read as
# intent rather than as SQL trivia.
CHILD_CREDENTIAL_INSERT = "INSERT INTO ai_credential"
MEMBERSHIP_LOOKUP = "WHERE ai_credential.managed_credential_id ="


@contextmanager
def failing_sql_statement(
    db: Session,
    *,
    when_statement_contains: str,
    occurrence: int = 1,
    injected_sql: str = BROKEN_SQL,
) -> Iterator[dict[str, Any]]:
    """Abort the transaction when a matching SQL statement is about to run.

    Installs a ``before_cursor_execute`` listener on the test engine. When the
    ``occurrence``-th statement containing ``when_statement_contains`` comes
    round, a deliberately invalid statement is executed on the *same* DBAPI
    cursor first. Postgres raises and marks the transaction aborted; the
    original statement never runs.

    This is fault injection at the database, not a patched collaborator: the
    production call stack is entirely real, and what it experiences is exactly
    what a lock timeout, a serialization failure or a dropped connection mid
    -statement would look like.

    ``occurrence`` matters more than it looks. With a single auto-provisioned
    parent the failure lands while ``user`` is still fresh from
    ``create_account``'s ``refresh``, so every attribute the failure handler
    might touch is already loaded and broken code passes. Aiming the injection
    at the *second* parent — after one child has committed and expired the
    identity map — is what makes the test able to fail.

    Yields a small state dict (``{"seen": int, "fired": bool}``) so a test can
    assert the injection actually happened rather than assume it.
    """
    engine = db.get_bind().engine
    state: dict[str, Any] = {"seen": 0, "fired": False}
    needle = " ".join(when_statement_contains.split()).upper()

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):  # noqa: ANN001 - SQLAlchemy event signature
        if state["fired"]:
            return
        if needle not in " ".join(str(statement).split()).upper():
            return
        state["seen"] += 1
        if state["seen"] < occurrence:
            return
        state["fired"] = True
        cursor.execute(injected_sql)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield state
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)


def abort_transaction(db: Session) -> None:
    """Leave ``db``'s transaction in Postgres' aborted state, and prove it.

    Used to hand an already-broken session to code that promises to cope. The
    verification is not ceremony: if the statement quietly succeeded, or if
    SQLAlchemy recovered on its own, the test that follows would pass while
    exercising the healthy path.
    """
    try:
        db.execute(text(BROKEN_SQL))
    except Exception:
        pass
    else:  # pragma: no cover - would mean BROKEN_SQL stopped being broken
        raise AssertionError(
            f"{BROKEN_SQL!r} did not fail — the injection is inert."
        )

    if session_is_usable(db):  # pragma: no cover - defensive
        raise AssertionError(
            "The session recovered on its own; the aborted-transaction case "
            "is not being reproduced."
        )


def session_is_usable(db: Session) -> bool:
    """Whether ``db`` can still run a statement.

    False while the Postgres transaction is aborted *or* while SQLAlchemy is
    holding a pending rollback — the two shapes the failure takes, and the two
    the caller's next ``commit()`` would die on.
    """
    try:
        db.execute(text("SELECT 1")).one()
        return True
    except Exception:
        return False


def get_user_row(db: Session, user_id: str | uuid.UUID) -> User:
    """Load a ``User`` row so it can be handed to the provisioning service."""
    row = db.get(User, uuid.UUID(str(user_id)))
    assert row is not None, f"No user row for {user_id}"
    return row


def provision_account(
    db: Session, user: User, origin: AccountOrigin = AccountOrigin.SIGNUP
) -> Any:
    """Call ``AccountProvisioningService.on_account_created`` directly.

    The one caller in production is ``UserService.create_account``, reached
    only through an account-creating request. That request always hands over a
    *healthy* session, so the "already aborted" case has no HTTP surface at
    all — this is the only way to present it.
    """
    return AccountProvisioningService.on_account_created(db, user, origin)


def create_account_via_service(
    db: Session,
    *,
    email: str,
    origin: AccountOrigin,
    password: str | None = None,
    role: str | None = None,
    is_superuser: bool = False,
) -> User:
    """Create an account at the chokepoint under an origin no route carries.

    Exists for exactly one origin: ``seed``. ``AccountOrigin.SEED`` is used by
    ``core.db.init_db`` when an instance bootstraps its first superuser, which
    happens once, before any admin has configured anything, and never again —
    there is no request that can replay it. Every other origin in the enum is
    covered through its real HTTP path in ``tests/api/users/``, and must stay
    that way; this helper is not a shortcut for those.
    """
    from app.services.users.user_service import UserService

    return UserService.create_account(
        db,
        email=email,
        origin=origin,
        password=password,
        role=role,
        is_superuser=is_superuser,
    )
