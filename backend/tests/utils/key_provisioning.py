"""Driving the key-provisioning converge pass from a test.

RULE-1 EXEMPTION, DOCUMENTED
---------------------------
Same shape as ``tests/utils/account_provisioning.py``: this reaches a seam below
HTTP, so it lives in one named module rather than being re-derived in every test
file that needs it.

The seam is not an accident of testing. The converge pass is driven by a
scheduler, and the scheduler never runs under pytest (``app/main.py`` gates every
one of them on ``settings.TESTING``, for good reason — a job firing mid-run would
mutate the dev database from inside a test). So ``converge(session)`` taking a
session and being directly awaitable **is** the test surface, deliberately, and a
converge only reachable through its scheduler would be a converge nobody could
test.
"""
from __future__ import annotations

import asyncio

from sqlmodel import Session

from app.services.credentials.key_provisioning_service import (
    ConvergeReport,
    key_provisioning_service,
)

__all__ = [
    "ConvergeReport",
    "converge_keys",
    "make_membership_due",
    "mark_membership_minting",
    "membership_id_for",
]


def converge_keys(db: Session, *, limit: int | None = None) -> ConvergeReport:
    """Run one converge pass against the test session, synchronously.

    ``asyncio.run`` from the test thread, the same way the background-task
    collector drains what it captured. The pass must run inside whatever provider
    stub the test installed, so call it inside that block.
    """
    return asyncio.run(key_provisioning_service.converge(db, limit=limit))


def make_membership_due(db: Session, user_id) -> None:
    """Clear one membership's backoff so the next converge pass picks it up.

    The alternative is a test that sleeps out a real backoff, or one that reaches
    into the service to shorten its own constants — the second of which would
    stop the test from exercising the constants that actually ship.

    Only the scheduled time is cleared. The attempt counter, the status, the
    stored provider handles and everything else stay exactly as the code left
    them, so what the next pass sees is the real row, mid-backoff, and not a row
    this helper reset.
    """
    from sqlmodel import select

    from app.models.credentials.managed_ai_credential_membership import (
        ManagedAICredentialMembership,
    )

    membership = db.exec(
        select(ManagedAICredentialMembership).where(
            ManagedAICredentialMembership.user_id == user_id
        )
    ).first()
    assert membership is not None, f"no membership row for user {user_id}"
    membership.next_attempt_at = None
    db.add(membership)
    db.commit()


def mark_membership_minting(db: Session, user_id) -> None:
    """Put one membership into the ``minting`` state, as a claimed row looks.

    Reproduces the exact window a converge pass occupies while it is inside a
    provider call: the row is claimed and committed, and the mint has not
    returned. Simulating it is the only way to test what a *concurrent* admin
    action does to that row — a real overlap would need two sessions racing a
    network call.
    """
    from sqlmodel import select

    from app.models.credentials.managed_ai_credential_membership import (
        ManagedAICredentialMembership,
        MembershipProvisioningStatus,
    )

    membership = db.exec(
        select(ManagedAICredentialMembership).where(
            ManagedAICredentialMembership.user_id == user_id
        )
    ).first()
    assert membership is not None, f"no membership row for user {user_id}"
    membership.status = MembershipProvisioningStatus.MINTING.value
    membership.provision_attempts = 1
    db.add(membership)
    db.commit()


def membership_id_for(db: Session, user_id, parent_id=None) -> str:
    """The membership row's id, as a string.

    Two things address a membership directly and neither can reach it through
    the API: the provider-side key name is postfixed with this id, and every
    per-key verb (`/admin/ai-credentials/keys/{membership_id}/…`) takes one. It
    is not on any record projection — the admin member list publishes the
    *user*, which is the question that surface answers — so tests read it here
    rather than a field being invented for them.

    ``parent_id`` disambiguates a user who is a member of more than one record;
    without it the lookup asserts there is exactly one.
    """
    from sqlmodel import select

    from app.models.credentials.managed_ai_credential_membership import (
        ManagedAICredentialMembership,
    )

    statement = select(ManagedAICredentialMembership).where(
        ManagedAICredentialMembership.user_id == user_id
    )
    if parent_id is not None:
        statement = statement.where(
            ManagedAICredentialMembership.managed_credential_id == parent_id
        )
    rows = db.exec(statement).all()
    assert rows, f"no membership row for user {user_id}"
    assert len(rows) == 1, (
        f"user {user_id} is a member of {len(rows)} records; pass parent_id"
    )
    return str(rows[0].id)
