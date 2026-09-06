"""AccountProvisioningService — what a brand-new account is given, once.

WHAT THIS IS
------------
``UserService.create_account`` decides that an account *exists*. This decides
what is already in it when its owner first signs in. Today that means one
thing: the managed AI credentials an administrator marked
``auto_provision_roles ∋ user.role``, materialised as ordinary per-user child
``AICredential`` rows through the same ``add_members`` path the admin UI uses.
An employee who signs in with Google therefore lands on a dashboard that
already has the company key wired as their default, and Cinna Desktop picks
the same keys up through ``/external/account-config`` with no further step.

It is a separate service, and not four lines inside ``create_account``,
because it is the seam the later phases extend — per-user key minting (phase
5), identity-provider group mapping, licence assignment. Those all answer
"what does this person get", never "may this person exist", and the two
questions have different failure semantics:

TWO INVARIANTS THIS FILE IS BUILT AROUND
----------------------------------------
**1. Account creation never fails because provisioning failed.** A person
cannot be told "your account could not be created" because an admin pasted an
expired OpenAI key last month. So ``on_account_created`` has no failing exit:
every parent is attempted inside its own ``try``, a failure is recorded in the
returned report, written to the security feed and logged at warning, and the
loop moves on. The one caller ignores the return value. That is not sloppiness
— it is the point. Making the report a value rather than an exception is what
lets tests assert on failures without the production path ever branching on
one.

ERROR HANDLING
--------------
Two rules, both learned the hard way, both non-obvious enough to state:

**Repair the session before touching it.** A failed provisioning attempt can
leave the transaction aborted, and in that state *every* session operation
raises — including the lazy attribute load behind an innocent-looking
``logger.warning("... %s", user.id)``. A handler that logs before it rolls
back therefore throws from inside the handler, and the net above it does the
same, and the exception escapes every net that was written to catch it. So:
identifiers are snapshotted into locals while the session is known good,
``_restore_session`` runs first, and only then does anything get logged. (The
first parent in the loop hides this — ``user`` is still fresh from
``create_account``'s refresh; it is the second one, after a child credential
has committed and expired everything, that bites.)

**Roll back unconditionally.** See :meth:`_restore_session` for why the
obvious guard on ``session.is_active`` is worse than no guard.

The caller still has work to do after this returns — ``register_user`` sends a
confirmation email, which commits — so leaving the session unusable would fail
the account creation just as surely as re-raising would.

**2. No third-party provider is contacted on this path.** Every key handed out
here is already in the database, decrypted from the parent record. Nothing in
this module talks to OpenAI, Anthropic or a Google admin API, and nothing
should: this runs inline on the signup and OAuth-callback request, where a
provider timeout would become a failed login. Phase 5 introduces minting and
it goes in a background task, not here.

WHY THE EVENTS ARE WRITTEN DIRECTLY
-----------------------------------
``SecurityEventService.create_event`` is ``async`` (its body is not — it
awaits nothing — but the signature is). This path is synchronous and is called
from inside both sync and async routes, so there is no loop to schedule it on
and no safe way to make one. The rows are therefore constructed here, exactly
as ``UserService.disable_all_factors`` already does, with the same shape the
admin route's ``admin.ai_credential.provision`` event uses so both kinds of
grant read alike in a user's security feed.

The events are scoped to the **new owner** (``security_event.user_id`` is NOT
NULL, and it is their feed the grant belongs in); the *actor* is the system,
recorded as ``details.actor = "system"`` alongside the origin and the
managing admin of the parent record. No key material ever goes in ``details``.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models.credentials.managed_ai_credential import ManagedAICredential
from app.models.events.security_event import SecurityEvent
from app.models.users.user import AccountOrigin, User
from app.utils import restore_session

logger = logging.getLogger(__name__)


# Event types. Deliberately siblings of the admin route's
# ``admin.ai_credential.provision`` — same namespace, different actor — so a
# reader of the security feed can tell an automatic grant from an admin's
# deliberate one without decoding the details blob.
EVENT_AUTO_PROVISION = "admin.ai_credential.auto_provision"
EVENT_AUTO_PROVISION_FAILED = "admin.ai_credential.auto_provision_failed"


@dataclass(frozen=True)
class ProvisionedCredential:
    """One managed credential successfully granted to the new account."""

    managed_credential_id: uuid.UUID
    child_credential_id: uuid.UUID


@dataclass(frozen=True)
class ProvisioningSkip:
    """One managed credential that was *not* granted, and why.

    ``reason`` is a stable machine-readable string: the reconcile skip reasons
    (``user_not_found``, ``user_inactive``, ``provision_failed``) or
    ``add_members_failed`` when the call itself raised.
    """

    managed_credential_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class ProvisioningReport:
    """Outcome of provisioning one account. Never an error, always a value."""

    added: list[ProvisionedCredential] = field(default_factory=list)
    skipped: list[ProvisioningSkip] = field(default_factory=list)


class AccountProvisioningService:
    """What a new account receives. See the module docstring."""

    @staticmethod
    def on_account_created(
        session: Session, user: User, origin: AccountOrigin
    ) -> ProvisioningReport:
        """Grant every managed AI credential whose roles include ``user.role``.

        Called by ``UserService.create_account`` after the account row is
        committed. Never raises — unconditionally, including when handed a
        session whose transaction is already aborted. That is why the
        identifier snapshot is *inside* the ``try`` and ``user_id`` is
        pre-bound to ``None``: reading ``user.id`` is itself a session
        operation, so a snapshot taken above the ``try`` would be the one line
        in this function that could throw past every net. Today's only caller
        commits and refreshes immediately beforehand and could not trigger
        that; phase 3's invite path is a new caller, and a promise that holds
        only for the callers that exist is not one worth documenting.

        Role changes *after* creation deliberately do not re-run this — an
        admin promoting someone should not silently hand them a company key as
        a side effect. The explicit "Apply to existing users" action on the
        managed credential is the intended path for that.
        """
        # ``origin`` is a plain enum — reading it cannot touch the session, so
        # it is safe out here and always available to the handler.
        origin_value = origin.value
        user_id: uuid.UUID | None = None
        try:
            # See ERROR HANDLING in the module docstring: on a broken session
            # even ``user.id`` raises, so the read is guarded like any other.
            user_id = user.id
            return AccountProvisioningService._provision(session, user, origin)
        except Exception:
            # The outer net. Everything below is *also* guarded per parent,
            # but the prologue — the deferred import, the parents query, the
            # role filter — was not, and an exception there would escape into
            # a caller that has already committed the account row. A signup
            # answering 500 for an account that exists is precisely the
            # failure invariant 2 forbids, so the guarantee is made
            # structural here rather than left to the discipline of the code
            # inside.
            AccountProvisioningService._restore_session(session)
            logger.warning(
                "Auto-provisioning for new user %s (origin=%s) failed before "
                "it could report per-credential results; the account was "
                "still created.",
                user_id,
                origin_value,
                exc_info=True,
            )
            return ProvisioningReport()

    @staticmethod
    def _provision(
        session: Session, user: User, origin: AccountOrigin
    ) -> ProvisioningReport:
        """The body of :meth:`on_account_created`, minus the outer net."""
        from app.services.credentials.managed_ai_credentials_service import (
            managed_ai_credentials_service,
        )

        added: list[ProvisionedCredential] = []
        skipped: list[ProvisioningSkip] = []

        # Snapshot first — before any other read of ``user``, including the
        # ``is_active`` test below. Every one of those reads can hit the
        # session, and the whole point of the locals is that the failure paths
        # never have to. Anything that throws here is caught by
        # ``on_account_created``'s outer net.
        user_id = user.id
        user_role = user.role
        origin_value = origin.value

        if not user.is_active:
            # An admin creating a deactivated account is a normal act, not a
            # provisioning failure. Granting keys to an account nobody can
            # sign into buys nothing, and reporting the inevitable
            # ``user_inactive`` skip would write a medium-severity security
            # event per managed credential into that account's feed. When the
            # account is activated, "Apply to existing users" is the path.
            return ProvisioningReport()

        # Filtered in Python rather than with a JSON containment predicate:
        # the table holds a handful of rows per instance, and a portable
        # ``?|``/``@>`` expression over a ``json`` (not ``jsonb``) column is
        # more machinery than the saving is worth. ``isinstance`` guards a
        # hand-edited row whose JSON is not a list — ``in`` against a
        # non-container raises, and this runs on the signup path.
        parents = [
            parent
            for parent in session.exec(select(ManagedAICredential)).all()
            if isinstance(parent.auto_provision_roles, list)
            and user_role in parent.auto_provision_roles
        ]
        if not parents:
            return ProvisioningReport()

        for parent in parents:
            parent_id = parent.id
            managed_by_id = (
                str(parent.managed_by_id) if parent.managed_by_id else None
            )
            try:
                addition = managed_ai_credentials_service.add_members(
                    session,
                    parent=parent,
                    user_ids=[user_id],
                    actor=None,
                )
            except Exception:
                # Invariant 1: the account survives whatever went wrong here.
                # Repair first, log second — the logging call is itself a
                # session operation if any of its arguments is an ORM
                # attribute, which is why they are all locals by now.
                AccountProvisioningService._restore_session(session)
                logger.warning(
                    "Auto-provisioning managed credential %s for new user %s "
                    "(origin=%s) failed; the account was still created.",
                    parent_id,
                    user_id,
                    origin_value,
                    exc_info=True,
                )
                skipped.append(
                    ProvisioningSkip(
                        managed_credential_id=parent_id,
                        reason="add_members_failed",
                    )
                )
                AccountProvisioningService._emit(
                    session,
                    user_id=user_id,
                    event_type=EVENT_AUTO_PROVISION_FAILED,
                    severity="medium",
                    details={
                        "managed_credential_id": str(parent_id),
                        "target_user_id": str(user_id),
                        "origin": origin_value,
                        "role": user_role,
                        "reason": "add_members_failed",
                        "actor": "system",
                    },
                )
                continue

            for member in addition.added:
                added.append(
                    ProvisionedCredential(
                        managed_credential_id=parent_id,
                        child_credential_id=member.child_credential_id,
                    )
                )
                AccountProvisioningService._emit(
                    session,
                    user_id=user_id,
                    event_type=EVENT_AUTO_PROVISION,
                    severity="low",
                    details={
                        "managed_credential_id": str(parent_id),
                        "child_credential_id": str(member.child_credential_id),
                        "target_user_id": str(user_id),
                        "origin": origin_value,
                        "role": user_role,
                        "managed_by_id": managed_by_id,
                        "actor": "system",
                    },
                )

            for skip in addition.skipped:
                logger.warning(
                    "Auto-provisioning managed credential %s for new user %s "
                    "(origin=%s) was skipped: %s",
                    parent_id,
                    user_id,
                    origin_value,
                    skip.reason,
                )
                skipped.append(
                    ProvisioningSkip(
                        managed_credential_id=parent_id, reason=skip.reason
                    )
                )
                AccountProvisioningService._emit(
                    session,
                    user_id=user_id,
                    event_type=EVENT_AUTO_PROVISION_FAILED,
                    severity="medium",
                    details={
                        "managed_credential_id": str(parent_id),
                        "target_user_id": str(user_id),
                        "origin": origin_value,
                        "role": user_role,
                        "reason": skip.reason,
                        "actor": "system",
                    },
                )

        return ProvisioningReport(added=added, skipped=skipped)

    @staticmethod
    def on_account_deactivated(session: Session, user: User) -> None:
        """Placeholder for the deactivation side of provisioning.

        Intentionally a no-op in phase 2. Shared managed credentials are one
        key held by many people, so deactivating one holder must NOT revoke it
        — their child row simply stops being reachable with the account. There
        is nothing to undo.

        Phase 5 changes that: once a key can be *minted per user*, that user's
        key has no other holder and deactivation should revoke it at the
        provider. This function is the declared place for that, named and
        wired now so the phase-5 change is a body and not a hunt for every
        caller that deactivates an account.
        """
        return None

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _restore_session(session: Session) -> None:
        """Make ``session`` usable again after a failed provisioning attempt.

        The mechanism — and why the rollback is unconditional rather than
        guarded on ``session.is_active`` — lives in :func:`app.utils.restore_session`,
        shared with ``ManagedAICredentialsService.add_members``, which is the
        other end of this same path and repairs its own failures for the same
        reason. Both failure classes described there are reachable from here;
        the next commit that would die on them is the caller's:
        ``register_user`` commits again to send its confirmation email.

        What is local to this service is the *safety* argument, which
        ``restore_session`` deliberately leaves to its callers. Rolling back
        here loses nothing by construction: ``create_account`` commits the
        account row *before* calling in, and ``add_members`` commits each
        successful child individually, so anything still pending when a
        provisioning attempt fails belongs to that failed attempt. Under the
        test fixture (``tests/conftest.py`` binds with
        ``join_transaction_mode="create_savepoint"``) the rollback unwinds to
        the session's own savepoint rather than the outer transaction, so it
        cannot take earlier committed test data with it either.
        """
        restore_session(session)

    @staticmethod
    def _emit(
        session: Session,
        *,
        user_id: uuid.UUID,
        event_type: str,
        severity: str,
        details: dict[str, object],
    ) -> None:
        """Write one security event. Best-effort — never raises.

        An audit row that cannot be written must not be the thing that breaks
        an account creation the caller was told could not fail.
        """
        try:
            session.add(
                SecurityEvent(
                    user_id=user_id,
                    event_type=event_type,
                    severity=severity,
                    details=json.dumps(details),
                )
            )
            session.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to record security event %s for user %s.",
                event_type,
                user_id,
            )
            AccountProvisioningService._restore_session(session)


__all__ = [
    "AccountProvisioningService",
    "ProvisionedCredential",
    "ProvisioningReport",
    "ProvisioningSkip",
    "EVENT_AUTO_PROVISION",
    "EVENT_AUTO_PROVISION_FAILED",
]
