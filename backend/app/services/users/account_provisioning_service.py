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
expired OpenAI key last month. So neither entry point has a failing exit:
every parent is attempted inside its own ``try``, a failure is recorded in the
returned report, written to the security feed and logged at warning, and the
loop moves on. Making the report a value rather than an exception is what lets
tests assert on failures without the production path ever branching on one.

There are two entry points and one body. :meth:`on_account_created` is the
automatic path — every managed credential whose ``auto_provision_roles``
contains the new account's role — and :meth:`provision_explicit` is the
invitation wizard's, granting a list the administrator chose. They share
:meth:`_provision` and :meth:`_guarded` rather than existing as two
implementations, because the moment they are two, an invited ``agent-user``
and a Google-arriving ``agent-user`` can end up with different key sets,
different audit events and different failure semantics, and nothing reports
the difference.

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
NULL, and it is their feed the grant belongs in). ``details.actor`` says who
caused it: the string ``"system"`` on the automatic path, where there is no
administrator in the story, or the acting superuser's id on the explicit one.
It sits alongside the origin and the managing admin of the parent record. No
key material ever goes in ``details``.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

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

# What the two entry points call themselves in the log. Passed explicitly
# rather than inferred from ``actor``, because these strings are what an
# administrator greps for: a failed invitation logged as "Auto-provisioning
# for new user" sends them looking through signups. The audit *event* type is
# deliberately shared — one feed entry shape for "this account did not get a
# credential it was meant to get", whichever door it arrived through.
_LABEL_AUTOMATIC = "Automatic provisioning"
_LABEL_INVITE = "Invitation provisioning"


@dataclass(frozen=True)
class ProvisionedCredential:
    """One managed credential successfully granted to the new account."""

    managed_credential_id: uuid.UUID
    child_credential_id: uuid.UUID


@dataclass(frozen=True)
class ProvisioningSkip:
    """One managed credential that was *not* granted, and why.

    ``reason`` is a stable machine-readable string: the reconcile skip reasons
    (``user_not_found``, ``user_inactive``, ``provision_failed``),
    ``add_members_failed`` when the call itself raised, or
    ``managed_credential_not_found`` when an explicitly requested id no longer
    names a record (only reachable from :meth:`provision_explicit`).

    ``user_inactive`` has a **second** producer, and it is the one a caller
    actually sees: :meth:`AccountProvisioningService._provision`'s inactive
    short-circuit emits it directly, one per requested credential. It returns
    before ``add_members`` is called, so on an inactive account the reconcile
    path's own ``user_inactive`` is unreachable.
    """

    managed_credential_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class ProvisioningReport:
    """Outcome of provisioning one account. Never an error, always a value.

    ``failed`` is the one thing ``added`` and ``skipped`` cannot express.
    :meth:`AccountProvisioningService._guarded` catches everything, so a total
    failure — a broken session, a raise in the prologue before any credential
    was even selected — returns a report that is *byte-identical to a
    deliberate grant of nothing*: empty, empty. A caller rendering that says
    "no credentials were granted", which is true and useless, and an admin who
    unticked every box sees the same screen as one whose provisioning fell
    over. This flag is what separates "nothing was asked for" from "we could
    not tell you what happened".

    It is deliberately not a :class:`ProvisioningSkip` entry: a skip names a
    credential, and the failure this describes may have happened before any
    credential was named.
    """

    added: list[ProvisionedCredential] = field(default_factory=list)
    skipped: list[ProvisioningSkip] = field(default_factory=list)
    failed: bool = False


class AccountProvisioningService:
    """What a new account receives. See the module docstring."""

    @staticmethod
    def on_account_created(
        session: Session, user: User, origin: AccountOrigin
    ) -> ProvisioningReport:
        """Grant every managed AI credential whose roles include ``user.role``.

        Called by ``UserService.create_account`` after the account row is
        committed. Never raises — unconditionally, including when handed a
        session whose transaction is already aborted; :meth:`_guarded` is
        where that promise is kept, and its docstring explains why even
        reading ``user.id`` has to happen inside the net.

        Role changes *after* creation deliberately do not re-run this — an
        admin promoting someone should not silently hand them a company key as
        a side effect. The explicit "Apply to existing users" action on the
        managed credential is the intended path for that.
        """
        return AccountProvisioningService._guarded(
            session,
            user,
            origin,
            lambda: AccountProvisioningService._provision(
                session,
                user,
                origin,
                parent_ids=None,
                actor=None,
                label=_LABEL_AUTOMATIC,
            ),
            label=_LABEL_AUTOMATIC,
        )

    @staticmethod
    def provision_explicit(
        session: Session,
        user: User,
        origin: AccountOrigin,
        *,
        managed_credential_ids: list[uuid.UUID] | None,
        actor: User,
    ) -> ProvisioningReport:
        """Grant a caller-chosen set of managed AI credentials to a new account.

        The invitation wizard's entry point, and the *only* sanctioned way for
        an explicit list to be granted at account creation. It exists as a
        method here rather than as a loop over ``add_members`` in the invite
        route because everything that makes the automatic path safe is in this
        module and none of it is in a route:

        * the outer never-fail net (invariant 1 — an invitation must not fail
          because an admin pasted an expired key last month);
        * ``_restore_session`` before any post-failure logging, which is what
          stops a ``PendingRollbackError`` escaping past every net;
        * the ``ProvisioningReport`` skip list and the per-credential
          ``SecurityEvent`` written into the *invited* account's feed;
        * the ``not user.is_active`` short-circuit, so inviting a deactivated
          account grants nothing and writes no skip events.

        A hand-rolled loop reimplements all four, badly, and the failure is
        invisible: the invite succeeds, the account exists, and the person
        signs in with no company key.

        ``managed_credential_ids`` distinguishes "not stated" from "none".
        ``None`` means *grant exactly what this role would have been granted
        automatically*, evaluated by the same predicate
        :meth:`_provision` uses — so an invited ``agent-user`` and a
        Google-arriving ``agent-user`` cannot end up with different key sets.
        ``[]`` means the admin deliberately unticked everything.

        ``actor`` is the acting superuser and is required. Unlike the
        automatic path there *is* an admin in this story, and the grant is
        attributed to them in both ``add_members`` and the audit event.
        """
        return AccountProvisioningService._guarded(
            session,
            user,
            origin,
            lambda: AccountProvisioningService._provision(
                session,
                user,
                origin,
                parent_ids=managed_credential_ids,
                actor=actor,
                label=_LABEL_INVITE,
            ),
            label=_LABEL_INVITE,
        )

    @staticmethod
    def _guarded(
        session: Session,
        user: User,
        origin: AccountOrigin,
        run: Callable[[], ProvisioningReport],
        *,
        label: str,
    ) -> ProvisioningReport:
        """The outer never-fail net, shared by both entry points.

        Factored out so the two entry points cannot drift: a second copy of
        this is a second chance to forget ``_restore_session``, or to log
        ``user.id`` before the repair and throw from inside the handler.

        ``user_id`` is pre-bound to ``None`` and read *inside* the ``try``
        because reading it is itself a session operation — on an aborted
        transaction it is a query, so a snapshot taken above the ``try`` would
        be the one line here that could throw past every net.

        ``label`` names the calling path in the log line. It is required and
        keyword-only for the same reason ``add_members``' ``actor`` is: with a
        default, the invite path would silently keep reporting itself as
        automatic provisioning and an admin debugging a failed invitation
        would grep for a string that is never written.
        """
        # ``origin`` is a plain enum — reading it cannot touch the session, so
        # it is safe out here and always available to the handler.
        origin_value = origin.value
        user_id: uuid.UUID | None = None
        try:
            # See ERROR HANDLING in the module docstring: on a broken session
            # even ``user.id`` raises, so the read is guarded like any other.
            user_id = user.id
            return run()
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
                "%s for user %s (origin=%s) failed before it could report "
                "per-credential results; the account was still created.",
                label,
                user_id,
                origin_value,
                exc_info=True,
            )
            # ``failed=True``, not a bare report: see :class:`ProvisioningReport`.
            # An empty report is what a deliberate grant of nothing also looks
            # like, and the two must not render identically.
            return ProvisioningReport(failed=True)

    @staticmethod
    def _provision(
        session: Session,
        user: User,
        origin: AccountOrigin,
        *,
        parent_ids: list[uuid.UUID] | None,
        actor: User | None,
        label: str,
    ) -> ProvisioningReport:
        """The body of both entry points, minus the outer net.

        ``parent_ids is None`` selects the automatic set (every managed
        credential whose ``auto_provision_roles`` contains the account's role);
        a list selects exactly those records. Everything after the selection —
        the per-parent guard, the skip reporting, the audit events — is
        identical for both, which is the point of having one function. The
        inactive short-circuit runs *before* the selection and is the one
        place the two differ: it can only name the credentials it was handed,
        so the explicit path reports one skip each and the automatic path has
        nothing to name. Its comment says why.

        ``actor`` is ``None`` for the system-initiated path and the acting
        superuser for an explicit grant. It reaches ``add_members`` and the
        audit event's ``details.actor``; it does not change what is written to
        the child, which is stamped with the *parent's* managing admin either
        way.
        """
        from app.services.credentials.managed_ai_credentials_service import (
            managed_ai_credentials_service,
        )

        added: list[ProvisionedCredential] = []
        skipped: list[ProvisioningSkip] = []

        # Snapshot first — before any other read of ``user``, including the
        # ``is_active`` test below. Every one of those reads can hit the
        # session, and the whole point of the locals is that the failure paths
        # never have to. Anything that throws here is caught by
        # ``_guarded``'s outer net.
        user_id = user.id
        user_role = user.role
        origin_value = origin.value
        # Same rule for the actor: the failure handlers below name it in an
        # audit row, and by then the session may be unusable.
        actor_id = actor.id if actor is not None else None
        # ``details.actor`` distinguishes the two paths in the security feed:
        # ``"system"`` for a grant that happened because an account was
        # created, or the acting superuser's id for one an administrator
        # chose. The event *types* stay the same for both — this is still
        # provisioning at account creation, and ``details.origin`` already
        # says which arrival path it was.
        actor_detail = "system" if actor_id is None else str(actor_id)

        if not user.is_active:
            # An admin creating a deactivated account is a normal act, not a
            # provisioning failure. Granting keys to an account nobody can
            # sign into buys nothing, and no security event is written for
            # this: a medium-severity ``auto_provision_failed`` row per
            # managed credential, in the feed of an account that has done
            # nothing, is noise about an outcome that was never in doubt. When
            # the account is activated, "Apply to existing users" is the path.
            #
            # But the *report* must still say what happened, for the same
            # reason ``ProvisioningReport.failed`` exists: a bare empty report
            # is byte-identical to a deliberate grant of nothing, so an admin
            # who ticked three credentials and invited a deactivated account
            # saw exactly the screen of an admin who ticked none. One skip per
            # requested credential, with the reason the vocabulary already has
            # — ``user_inactive``, the string the shared reconcile path emits
            # for this same condition, so no new field and no new value.
            #
            # Only the explicit path can name them. On the automatic path
            # ``parent_ids`` is ``None``, the set is not known without the
            # query this short-circuit exists to skip, and no caller renders
            # that report at all — ``on_account_created``'s return value is
            # read by tests and by nothing else.
            return ProvisioningReport(
                skipped=[
                    ProvisioningSkip(
                        managed_credential_id=parent_id,
                        reason="user_inactive",
                    )
                    # Deduplicated here as well as below: the same id twice
                    # would produce one grant on the active path, so it must
                    # not produce two skips on this one.
                    for parent_id in dict.fromkeys(parent_ids or [])
                ]
            )

        if parent_ids is None:
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
        else:
            # Deduplicated because the same id twice would produce one grant
            # (``add_members`` is idempotent) and two audit events.
            wanted = list(dict.fromkeys(parent_ids))
            if not wanted:
                return ProvisioningReport()
            parents = list(
                session.exec(
                    select(ManagedAICredential).where(
                        col(ManagedAICredential.id).in_(wanted)
                    )
                ).all()
            )
            # An id the admin ticked that no longer names a record is
            # reported, not silently dropped: the wizard's list can go stale
            # against a concurrent deletion, and "you asked for four keys and
            # got three" is only visible if the fourth says why.
            found = {parent.id for parent in parents}
            for missing in wanted:
                if missing not in found:
                    skipped.append(
                        ProvisioningSkip(
                            managed_credential_id=missing,
                            reason="managed_credential_not_found",
                        )
                    )

        if not parents:
            return ProvisioningReport(added=added, skipped=skipped)

        for parent in parents:
            parent_id = parent.id
            managed_by_id = (
                str(parent.managed_by_id) if parent.managed_by_id else None
            )
            try:
                # Two call shapes rather than one ``actor=actor``, and
                # deliberately. ``actor=None`` means *the system did this, at
                # account creation* — the one attribution the audit trail
                # treats as special — and
                # ``tests/architecture/account_creation_chokepoint_test.py``
                # Rule 2 enumerates it **statically**: it looks for a literal
                # ``actor=None`` on an ``add_members`` call and asserts this
                # module is the only file in the backend that has one.
                # Collapsing these into a computed argument erases the thing
                # that test measures, and the rule goes quietly inert while
                # staying green.
                if actor is None:
                    addition = managed_ai_credentials_service.add_members(
                        session,
                        parent=parent,
                        user_ids=[user_id],
                        actor=None,
                    )
                else:
                    addition = managed_ai_credentials_service.add_members(
                        session,
                        parent=parent,
                        user_ids=[user_id],
                        actor=actor,
                    )
            except Exception:
                # Invariant 1: the account survives whatever went wrong here.
                # Repair first, log second — the logging call is itself a
                # session operation if any of its arguments is an ORM
                # attribute, which is why they are all locals by now.
                AccountProvisioningService._restore_session(session)
                logger.warning(
                    "%s of managed credential %s for user %s (origin=%s) "
                    "failed; the account was still created.",
                    label,
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
                        "actor": actor_detail,
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
                        "actor": actor_detail,
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
                        "actor": actor_detail,
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

        ``_restore_session`` runs **first** in the handler, before the log
        line — the module docstring's "repair the session before touching it"
        rule, which this handler is not exempt from merely because both of its
        format arguments happen to be plain locals today. The failing
        statement here is a ``commit`` on a session the caller keeps using, so
        this is precisely the handler where a repair that runs second, or not
        at all, leaves the caller's next commit to die somewhere unrelated. A
        later edit that adds ``user.role`` or an ORM attribute to the message
        must not be what re-discovers the ordering.
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
            AccountProvisioningService._restore_session(session)
            logger.exception(
                "Failed to record security event %s for user %s.",
                event_type,
                user_id,
            )


__all__ = [
    "AccountProvisioningService",
    "ProvisionedCredential",
    "ProvisioningReport",
    "ProvisioningSkip",
    "EVENT_AUTO_PROVISION",
    "EVENT_AUTO_PROVISION_FAILED",
]
