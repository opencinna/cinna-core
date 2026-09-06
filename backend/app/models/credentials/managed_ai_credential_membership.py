"""Membership of a managed AI credential — one row per (parent, user).

WHY THIS TABLE EXISTS
---------------------
Membership used to be **derived**: a user was a member of a parent record iff an
``AICredential`` row existed whose ``managed_credential_id`` pointed at it. That
worked while "member" and "holds a key" were the same fact.

Per-user key *minting* separates them. The moment an admin adds someone to a
minted parent, they are a member and they do not hold a key — the provider has
not been called yet, and it may fail. The derived model has nowhere to put that:
a row that does not exist cannot carry a status, and the alternative (a child
``AICredential`` holding an empty key) breaks the invariant every consumer of
that table relies on — **every ``AICredential`` row that exists is usable.**

So membership becomes a row of its own, for **every** parent, shared and minted
alike. Not "a second membership source beside the derived one": the derived
notion is gone. One definition, one place, one query. Two definitions of "who is
a member" is precisely the duplication this phase exists to remove, and it does
not become acceptable by being new.

The parent's own docstring used to say there was deliberately no membership
column, "to avoid drift between intended and actual members". That fear is real
and this row is the *cure* for it rather than an instance of it: the drift it
warns about is a desired-set column that nothing reconciles. This row is not a
desired set — it is the reconciled state, carrying an explicit status that says
which of the two it currently is, and it is written by the same reconcile that
creates and deletes the child.

TWO INVARIANTS
--------------
**1. The status is always an explicit named value — never inferred from NULL.**
Including the terminal ones: a shared parent's member is ``not_applicable``
(there is nothing to provision, and that is a decision, not an absence), and an
exhausted mint is ``failed``. Nobody reads a NULL and interprets it. The one
absence that means something is the *row*: no row = never a member.

**2. A failure is durable.** A ``failed`` membership is never deleted to tidy up.
"Account creation never fails because provisioning failed" (see
``AccountProvisioningService``) is only an honest promise if the failure is still
visible to an administrator afterwards. Retries are bounded and converge to
``provisioned`` or ``failed``; there is no backoff that reads as "still working"
forever.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlmodel import Column, Field, Index, SQLModel, Text, UniqueConstraint, col
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType


class MembershipProvisioningStatus(str, Enum):
    """Where this member's key stands. Every value is explicit and terminal-or-
    working; none of them is "unknown".

    - ``not_applicable`` — **terminal.** The parent holds one shared key and the
      member's child row already carries it. There is no provider call in this
      member's story and there never will be. Distinct from ``provisioned``
      because the two revoke differently: a shared key must NOT be revoked when
      one of its many holders leaves, a minted one must.
    - ``pending`` — a mint is owed. The converge pass will attempt it.
    - ``minting`` — an attempt is in flight (claimed by a converge pass). A row
      left here by a crashed process is re-attempted, and the attempt begins by
      revoking whatever ``external_key_ref`` holds so a key is never leaked.
    - ``provisioned`` — **terminal.** A key was minted and the child row holds
      it. ``external_key_ref`` carries the handles needed to revoke it.
    - ``failed`` — **terminal.** Bounded retries were exhausted. Durable and
      visible: an administrator must look at it. Never cleaned up automatically,
      and never reached by a row that is merely slow.
    - ``suspended`` — **terminal until reactivation.** The owner's account was
      deactivated, their minted key was revoked and their child row deleted, but
      they are still a member of the record. Reactivating the account puts the
      row back to ``pending`` and they are minted a fresh key. Deliberately not
      ``pending``: a pending row on a disabled account would read as "still
      working" for as long as the account stays disabled.
    """

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    MINTING = "minting"
    PROVISIONED = "provisioned"
    FAILED = "failed"
    SUSPENDED = "suspended"


#: Statuses a converge pass may pick up. Everything else is terminal for now.
CONVERGEABLE_STATUSES = (
    MembershipProvisioningStatus.PENDING.value,
    MembershipProvisioningStatus.MINTING.value,
)


class ManagedAICredentialMembership(SQLModel, table=True):
    """One user's membership of one managed AI credential parent record.

    Uniqueness on ``(managed_credential_id, user_id)`` is what makes "is this
    person a member" a single-row question with a single answer. It is also the
    guard the backfill migration relies on: a pre-existing duplicate child would
    fail the migration loudly rather than producing a member list that is quietly
    wrong.
    """

    __tablename__ = "managed_ai_credential_membership"
    __table_args__ = (
        UniqueConstraint(
            "managed_credential_id",
            "user_id",
            name="uq_managed_ai_credential_membership_parent_user",
        ),
        Index(
            "ix_managed_ai_cred_membership_status_next",
            "status",
            "next_attempt_at",
        ),
        Index("ix_managed_ai_cred_membership_user", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    managed_credential_id: uuid.UUID = Field(
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("managed_ai_credential.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    #: One of :class:`MembershipProvisioningStatus`. NOT NULL, no default in the
    #: application layer either — every writer states which one it means.
    status: str = Field(sa_column=Column(sa.String(24), nullable=False))

    #: The child credential this membership materialised, when one exists.
    #: ``SET NULL`` rather than ``CASCADE``: deleting the key must not delete the
    #: membership, because a suspended or failed member is still a member.
    ai_credential_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("ai_credential.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    #: Provider handles for a minted key: ``{project_id, service_account_id,
    #: api_key_id?, scopes_requested: [], scopes_granted: null}``. Written
    #: **before** the key is stored, so a process that dies mid-mint leaves
    #: behind the handles needed to revoke the orphan rather than a leaked key
    #: nobody can name. NULL on every shared membership.
    external_key_ref: dict | None = Field(
        default=None, sa_column=Column(PG_JSON, nullable=True)
    )

    #: How many mint attempts have been made. Bounded — see
    #: ``KeyProvisioningService.MAX_ATTEMPTS``.
    provision_attempts: int = Field(
        default=0,
        sa_column=Column(
            sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    #: When the next attempt becomes eligible. NULL means "eligible now" for a
    #: converge-able status, and means nothing at all for a terminal one — which
    #: is why the *status* is what a reader consults, never this column.
    next_attempt_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True)
    )
    #: Coarse reason code for the last failure (``mint_failed``,
    #: ``project_not_capped``, ``no_admin_credential``…). Never a provider
    #: response body, never key material.
    last_error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ============= The one "does this member hold a key" predicate =============
#
# ``external_key_ref IS NOT NULL`` rather than a status list, and the difference
# is not pedantry. A membership mid-mint holds a key the provider has already
# created, and a ``failed`` membership can hold one too — ``_settle`` and
# ``_record_failure`` never clear the ref, and two paths reach the attempt
# ceiling with it populated (``child_create_failed`` writes the ref and then
# fails; ``stale_<code>`` fails while an earlier attempt's ref is still on the
# row). The ref *is* the key's existence; the status is only where the row sits
# in its lifecycle.
#
# Declared here, on the row, because three unrelated callers ask it and answering
# it three times is how deactivation and deletion came to disagree about which
# members hold a key: the admin-secret usage count (may this credential be
# deleted), account deletion (which keys must be destroyed) and account
# deactivation (the same question, once asked by status instead and therefore
# answered wrongly for ``failed``).


def holds_provider_key(membership: "ManagedAICredentialMembership") -> bool:
    """Whether a key exists at the provider that only this row can name."""
    return bool(membership.external_key_ref)


def holds_provider_key_clause() -> Any:
    """The same predicate as a SQL clause, for callers that filter in the query.

    Not a convenience: a Python-side filter cannot be added to a ``select`` that
    counts, and a counting query that re-derived the rule would be the second
    implementation this pair exists to prevent.
    """
    return col(ManagedAICredentialMembership.external_key_ref).is_not(None)


# ============= Projection DTOs =============


class UserKeyProvisioningPublic(SQLModel):
    """One of *this user's* memberships that has no key behind it yet.

    The owner-facing counterpart of ``ManagedAICredentialMember``, and the only
    way a person can be told that a key is on its way. It has to be its own
    projection rather than an extra row in the credential list, because the list
    is ``AICredentialPublic`` and **every ``AICredential`` row that exists is
    usable** — an entry there with no key would break the one invariant every
    consumer of that table relies on.

    It is a *server* projection rather than a second list the browser folds into
    the first: the status is stated once, by the side that owns it.

    Only the states with no key are ever projected here (``pending``,
    ``minting``, ``failed``). ``provisioned`` and ``not_applicable`` are already
    in the credential list, and appearing in both is how one thing starts
    looking like two.
    """

    managed_credential_id: uuid.UUID
    #: The parent record's name — what the administrator called it.
    name: str
    #: The parent's provider. Typed, not a bare string: the admin projection
    #: publishes a union here and an owner-facing ``string`` would be a second,
    #: weaker answer to the same question in the generated client.
    type: AICredentialType
    status: MembershipProvisioningStatus
    #: Coarse reason code for the last failure. Never a provider response body
    #: and never key material. NULL unless something went wrong.
    last_error: str | None = None
    updated_at: datetime
