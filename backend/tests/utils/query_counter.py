"""Counting the SQL a request actually issues.

WHY THIS EXISTS, AND WHY IT IS NOT ``failing_sql_statement``
------------------------------------------------------------
``tests/utils/account_provisioning.py`` already installs a
``before_cursor_execute`` listener — but that one is a **fault injector**: it
counts matching statements only so it can decide which occurrence to break,
and it stops counting the moment it fires. Reusing its counter would give a
number that means "statements seen until the first injection", under a name
that says the opposite. So this is a separate facility on the same seam, with
the opposite contract: it observes and never injects.

WHAT IT IS FOR
--------------
List projections in this codebase have shipped N+1s more than once. The
``_assignment_to_public`` projection did it, and
``app/api/routes/_user_public.py``'s own comments call out the ``state.unloaded``
shortcut as the version of it that would turn ``GET /users/`` into exactly that
— a full-row SELECT per row of the admin users list. That list has since grown
a second batched lookup (``InvitationService.status_map``, one ``IN`` over the
page), and nothing in the suite could see the difference between one query and
one query per user.

A count is a weak assertion on its own — it passes just as happily when the
feature stops working altogether — so every caller pairs it with the
behavioural assertion for the same request. The count says *how*; the
behaviour says *whether*.

THE RULE-1 NOTE
---------------
Same exemption shape as ``account_provisioning.py`` and
``tests/utils/environment.py``: this reaches a seam below HTTP, so it lives in
one named module rather than being re-derived in test files. It is the cheaper
end of that bargain — it imports nothing from ``app`` at all, only SQLAlchemy's
event API and the engine behind the test session.

USAGE
-----
::

    with count_queries(db, matching="user_invitation") as statements:
        response = client.get(f"{API}/users/", headers=admin)
    assert len(statements) == 1, statements

The yielded list is the statements themselves, not a bare integer, so a
failure names the queries it counted instead of only how many there were.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session


@contextmanager
def count_queries(db: Session, *, matching: str) -> Iterator[list[str]]:
    """Record every SQL statement containing ``matching`` inside the block.

    Matching is case-insensitive and whitespace-normalised, so ``matching``
    can be a table name (``"user_invitation"``) or a fragment of a statement
    (``"INSERT INTO security_event"``) without having to reproduce the
    formatting SQLAlchemy happens to emit.

    The listener sits on the *engine*, so it sees statements issued by the
    request thread ``TestClient`` runs the app in as well as the test's own.
    """
    engine = db.get_bind().engine
    seen: list[str] = []
    needle = " ".join(matching.split()).upper()

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):  # noqa: ANN001 - SQLAlchemy event signature
        normalised = " ".join(str(statement).split())
        if needle in normalised.upper():
            seen.append(normalised)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
