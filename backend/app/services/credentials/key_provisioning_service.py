"""KeyProvisioningService — mint, revoke, and converge per-user provider keys.

WHAT IT OWNS
------------
Exactly one thing: the **provisioning lifecycle** of a membership row.
``pending → minting → provisioned | failed``, plus ``suspended`` when an account
is deactivated and back to ``pending`` when it is reactivated. It never creates
or deletes a membership — that is ``ManagedAICredentialsService``'s, because
"who is a member" and "does that member have a key yet" are two questions and
giving them one owner is how they get answered inconsistently.

TWO INVARIANTS THIS FILE IS BUILT AROUND
----------------------------------------
**1. Every ``AICredential`` row that exists is usable.** There is no
placeholder child, no empty key, no "preparing" credential. A membership carries
the pre-key state; the credential table is untouched until a real key exists.
Every consumer of that table — the discovery cron that probes every row, the
account-config bundle that ships every owned row to the desktop, the environment
credential bag — therefore needs no new filter and no new concept, and cannot
grow one by accident.

**2. Retries are bounded and converge.** ``MAX_ATTEMPTS`` attempts with a
lengthening backoff, then ``failed`` — durable, visible, and never deleted. A
row that retries forever reads as "still working" and hides an outage nobody is
paged for.

CRASH SAFETY, AND THE WINDOW THAT REMAINS
-----------------------------------------
The provider handles are committed **before** the key is stored, so a process
that dies mid-mint leaves a row that names the orphan it created. The next
attempt revokes that first and then mints again, which is why a crash costs a
wasted key rather than a leaked one.

The *other* half of that safety is a claim token. The converge leader lock
excludes another converge pass; it excludes no request handler. A deactivation,
an account deletion or a forced parent delete can all land inside the ``await``
on the provider, so **every write an attempt makes after taking the claim** —
the success path and the failure path alike — is a conditional
``UPDATE ... WHERE id = ? AND status = 'minting' AND updated_at = ?`` and a lost
claim means the key we are holding gets destroyed instead of stored. **A minted
key must never end up live-but-unrecorded**: if it cannot be stored it is
revoked, and if the revoke fails the durable event carries the external ref.

The failure path is conditional for a reason of its own, and it is not
symmetry. ``_record_failure`` writes ``pending`` with a retry time, so an
unconditional version resurrects whatever the row became while we were inside
the provider call: a deactivation that settled it ``suspended`` — a status whose
whole meaning is "this account is disabled and nothing should be minted for it"
— is overwritten by a failed mint that started before the deactivation and
finished after it. The next converge pass repairs it through the
``owner.is_active`` guard, so it costs a wasted attempt rather than a key; it
still means the row spent that window claiming to be queued work for a disabled
account, which is a state the status is documented as never having.

One window is genuinely unavoidable and is stated rather than papered over:
between the provider creating the service account and this process committing
anything, there is no record. The provider's create is a single call that returns
the secret — splitting it in two would mean passing
``create_service_account_only``, which makes the response carry no secret at all
— so a crash inside that window leaks one service account. It is visible in the
provider's own console and is not silent there; nothing we could write would
close it.

NO PROVIDER CALL ON A REQUEST PATH
----------------------------------
Adding a member records an intent and returns. Minting happens here, driven by
the converge scheduler. ``AccountProvisioningService`` runs inline on the signup
and OAuth-callback requests, and a provider timeout on that path would become a
failed login.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import sqlalchemy as sa
from fastapi import HTTPException
from sqlmodel import Session, col, or_, select

from app.core.db import create_session
from app.models.credentials.managed_ai_credential import ManagedAICredential
from app.models.credentials.managed_ai_credential_membership import (
    CONVERGEABLE_STATUSES,
    ManagedAICredentialMembership,
    MembershipProvisioningStatus,
    UserKeyProvisioningPublic,
    holds_provider_key,
    holds_provider_key_clause,
)
from app.models.credentials.provider_admin_credential import (
    AIProvider,
)
from app.models.events.security_event import SecurityEventCreate
from app.models.users.user import AIKeyOnboardingState, User
from app.services.ai_providers import registry
from app.services.ai_providers.base import (
    KeyProvisioner,
    ProviderAdminError,
    ProvisionScope,
)
from app.services.credentials.ai_credentials_service import (
    AICredentialInUseError,
    ai_credentials_service,
)
from app.services.credentials.key_provisioning_types import RevocationRequest
from app.services.credentials.managed_ai_credentials_service import (
    managed_ai_credentials_service,
)
from app.services.credentials.provisioning_policy import (
    resolve_policy,
)
from app.services.credentials.ai_providers_service import (
    ai_providers_service,
)
from app.services.events.security_event_service import SecurityEventService
from app.utils import create_task_with_error_logging, restore_session

logger = logging.getLogger(__name__)


# Event types, siblings of ``admin.ai_credential.provision`` so a reader of a
# user's security feed sees minted and shared grants in one shape. External
# refs are recorded deliberately — they are what makes a leaked key nameable —
# and key material never is.
EVENT_MINTED = "admin.ai_credential.minted"
EVENT_MINT_FAILED = "admin.ai_credential.mint_failed"
EVENT_REVOKED = "admin.ai_credential.revoked"
EVENT_REVOKE_FAILED = "admin.ai_credential.revoke_failed"
EVENT_REVOKE_BLOCKED = "admin.ai_credential.revoke_blocked"


@dataclass
class ConvergeReport:
    """What one converge pass did. A value, so tests read it instead of logs."""

    attempted: int = 0
    provisioned: int = 0
    failed: int = 0
    deferred: int = 0
    skipped: list[str] = field(default_factory=list)


class _MintClaim(NamedTuple):
    """One attempt's ownership of one membership row, as a token.

    ``(membership_id, status='minting', stamp)`` — the same shape
    ``status_repair_environments._Claim`` uses, and for the same reason. The
    status alone is not a claim: a row can leave ``minting`` and come back under
    a different attempt, and a status-only re-check would let the first attempt
    write over the second. The timestamp is what distinguishes "still the row I
    claimed" from "a row that happens to be in the same state again".

    ``user_id`` and ``parent_id`` ride along because the failure path needs them
    *after* the row may have ceased to exist: a revocation that cannot name its
    user and its parent record is a revocation nobody can audit.
    """

    membership_id: uuid.UUID
    user_id: uuid.UUID
    parent_id: uuid.UUID
    stamp: datetime


class KeyProvisioningService:
    """Mints, revokes and converges per-user provider keys."""

    #: Attempts before a membership goes terminal-``failed``. Bounded on purpose:
    #: the point of the ceiling is that somebody eventually has to look.
    MAX_ATTEMPTS = 5
    #: Seconds to wait before attempt N+1. The last value repeats if the list
    #: runs short, but it never does — the ceiling above is the real stop.
    BACKOFF_SECONDS = (60, 300, 900, 3600)
    #: How many memberships one converge pass will attempt. A pass is a provider
    #: round trip per member, so an unbounded batch is a scheduler tick that
    #: never ends.
    BATCH_SIZE = 25

    # ------------------------------------------------------------------ #
    # Converge
    # ------------------------------------------------------------------ #

    async def converge(
        self, session: Session, *, limit: int | None = None
    ) -> ConvergeReport:
        """Attempt every membership that is due a mint.

        Takes a session and is directly awaitable, which is the whole test
        surface: the scheduler never runs under pytest, so a converge that could
        only be reached through the scheduler could only be tested by not testing
        it.
        """
        now = datetime.now(timezone.utc)
        due = session.exec(
            select(ManagedAICredentialMembership)
            .where(
                col(ManagedAICredentialMembership.status).in_(
                    CONVERGEABLE_STATUSES
                ),
                or_(
                    col(ManagedAICredentialMembership.next_attempt_at).is_(None),
                    col(ManagedAICredentialMembership.next_attempt_at) <= now,
                ),
            )
            # NULLS FIRST, explicitly. Postgres sorts NULLs last on ASC, and a
            # NULL here means "never attempted" — so the default would queue every
            # brand-new member behind every backed-off retry, and a batch full of
            # failing rows would starve the people who just joined.
            .order_by(
                col(ManagedAICredentialMembership.next_attempt_at).asc().nullsfirst()
            )
            .limit(limit or self.BATCH_SIZE)
        ).all()

        report = ConvergeReport()
        for membership in due:
            report.attempted += 1
            try:
                await self._attempt(session, membership, report)
            except Exception:  # pragma: no cover - defensive
                # One member's failure must not end the pass. Repair before
                # logging: a statement-level failure leaves the transaction
                # aborted, and every argument to a log line that is an ORM
                # attribute is a query the handler cannot afford.
                restore_session(session)
                logger.exception("Key provisioning attempt failed unexpectedly")
                report.failed += 1
        return report

    async def _attempt(
        self,
        session: Session,
        membership: ManagedAICredentialMembership,
        report: ConvergeReport,
    ) -> None:
        """One mint attempt for one membership."""
        parent = session.get(
            ManagedAICredential, membership.managed_credential_id
        )
        owner = session.get(User, membership.user_id)
        if parent is None or owner is None:  # pragma: no cover - FK guarded
            report.skipped.append("row_missing")
            return

        if not resolve_policy(session, parent).is_minted:
            # A shared parent has no mint to attempt. Reachable only if a mode
            # ever changed under a live membership; say so in the row rather than
            # leaving it converge-able forever.
            self._settle(
                session,
                membership,
                status=MembershipProvisioningStatus.NOT_APPLICABLE,
                error=None,
            )
            report.skipped.append("parent_not_minted")
            return

        if not owner.is_active:
            # Not an attempt and not a failure: there is nobody to mint for. The
            # deactivation cascade normally sets this; this is the guard for a
            # deactivation that happened by another route.
            self._settle(
                session,
                membership,
                status=MembershipProvisioningStatus.SUSPENDED,
                error=None,
            )
            report.skipped.append("owner_inactive")
            return

        # Claim the row before anything that can fail, so a crash leaves evidence
        # that an attempt was in flight rather than a row that looks untouched.
        #
        # **Before** the configuration checks below, not after, and that ordering
        # is load-bearing: every path from here to the end of this method is an
        # *attempt*, so every one of them must cost an attempt. An earlier
        # version looked up the admin credential first and recorded a failure
        # without claiming — which meant a record wired to a deleted admin
        # credential retried every minute forever, never reached the ceiling, and
        # so never became ``failed``. That is precisely the state this design
        # refuses: an infinite backoff that reads as "still working".
        membership.status = MembershipProvisioningStatus.MINTING.value
        membership.provision_attempts += 1
        membership.updated_at = datetime.now(timezone.utc)
        session.add(membership)
        session.commit()
        session.refresh(membership)
        # ``(id, minting, updated_at)`` is now this attempt's claim *token*, and
        # every write after the provider call is conditional on it still holding.
        # The same shape the status-repair scheduler uses for exactly the same
        # reason (``status_repair_environments._Claim``): the leader lock excludes
        # other converge passes but excludes no request handler, so a
        # deactivation, an account deletion or a forced parent delete can land
        # inside our ``await`` and this row can stop being ours to write.
        claim = _MintClaim(
            membership_id=membership.id,
            user_id=membership.user_id,
            parent_id=parent.id,
            stamp=membership.updated_at,
        )

        admin_credential = (
            session.get(
                AIProvider, parent.provider_id
            )
            if parent.provider_id
            else None
        )
        adapter = registry.find_adapter(parent.type)
        provisioner = adapter.key_provisioner if adapter is not None else None
        if admin_credential is None or provisioner is None:
            if self._record_failure(session, claim, membership, "no_admin_credential"):
                await self._emit_mint_failed(
                    session, membership, parent, "no_admin_credential"
                )
                self._count_failure(membership, report)
            else:
                report.skipped.append("claim_lost")
            return

        secret = ai_providers_service.decrypt_secret(admin_credential)

        # Idempotency: a previous attempt that stored handles and then died left
        # a key we own and cannot use. Destroy it before minting another, or
        # every crash costs a key that stays live and unattributed.
        if membership.external_key_ref:
            stale_ref = dict(membership.external_key_ref)
            try:
                await provisioner.revoke(secret, stale_ref)
            except ProviderAdminError as exc:
                if self._record_failure(
                    session, claim, membership, f"stale_{exc.code}"
                ):
                    await self._emit_mint_failed(
                        session, membership, parent, f"stale_{exc.code}"
                    )
                    self._count_failure(membership, report)
                else:
                    # Nothing was minted this pass, so nothing leaks; whoever
                    # took the row inherits the stale ref and the same
                    # revoke-before-mint rule applies to them.
                    report.skipped.append("claim_lost")
                return
            cleared = self._write_under_claim(session, claim, external_key_ref=None)
            if cleared is None:
                # Somebody else settled this row while we destroyed its orphan.
                # Nothing was minted, so there is nothing to leak: stop.
                report.skipped.append("claim_lost")
                return
            claim = cleared

        # The name the *provider's* console shows for this key. It is read by a
        # human standing in OpenAI's UI asking "whose key is this?", so the
        # answer leads with the answer: the member's email address. The
        # membership id postfix is what makes it a handle rather than a hint —
        # it is unique per grant, so two keys for the same person (a re-mint
        # after a revoke, the same person in two providers) stay tellable apart,
        # and it is the row whose ``ai_credential_id`` names the credential this
        # key ends up on.
        #
        # The credential's own id cannot go here: it does not exist yet. The
        # child row is created from the minted secret, and no provider offers a
        # rename afterwards — OpenAI's administration API can create, list and
        # delete a service account but not modify one. The membership id is the
        # identifier that exists on both sides of the mint.
        label = f"{owner.email} ({membership.id})"
        try:
            minted = await provisioner.mint(
                secret,
                label=label,
                scope=ProvisionScope(
                    user_id=owner.id,
                    parent_id=parent.id,
                    config=admin_credential.config or {},
                ),
            )
        except ProviderAdminError as exc:
            if self._record_failure(session, claim, membership, exc.code):
                await self._emit_mint_failed(session, membership, parent, exc.code)
                self._count_failure(membership, report)
            else:
                report.skipped.append("claim_lost")
            return

        # ================================================================== #
        # A minted key must never end up live-but-unrecorded. If you cannot
        # store it, revoke it; if the revoke fails, emit the durable event with
        # the external ref in it.
        # ================================================================== #
        #
        # That is the invariant the whole state machine exists to protect, and
        # it is why both writes below are conditional on the claim and why
        # losing the claim goes to ``_discard_orphan_key`` rather than to a log
        # line. A key we hold in a local variable and nothing else names is the
        # one state from which no later pass — no retry, no deactivation, no
        # deletion sweep — can ever recover, because nothing knows it exists.

        # The handles first, in their own commit. The key is worthless to us
        # without them and dangerous without them to the provider.
        stored = self._write_under_claim(
            session, claim, external_key_ref=minted.external_ref
        )
        if stored is None:
            await self._discard_orphan_key(
                session, claim, parent, provisioner, secret, minted.external_ref
            )
            report.skipped.append("claim_lost")
            return
        claim = stored
        session.refresh(membership)

        try:
            child = managed_ai_credentials_service.materialise_minted_child(
                session, parent, owner, minted.api_key
            )
        except Exception:
            restore_session(session)
            logger.exception(
                "Minted a key for user %s but could not store it; the handles "
                "are recorded and the next attempt will revoke it before "
                "minting again.",
                claim.user_id,
            )
            if self._record_failure(
                session, claim, membership, "child_create_failed"
            ):
                await self._emit_mint_failed(
                    session, membership, parent, "child_create_failed"
                )
                self._count_failure(membership, report)
            else:
                # The handles were committed under the claim before this, so
                # whoever took the row can see the key and destroy it — the
                # deactivation path reads ``holds_provider_key`` and the
                # deletion path snapshots the ref. Nothing is unnamed.
                report.skipped.append("claim_lost")
            return

        settled = self._write_under_claim(
            session,
            claim,
            status=MembershipProvisioningStatus.PROVISIONED.value,
            last_error=None,
            next_attempt_at=None,
            ai_credential_id=child.id,
        )
        if settled is None:
            # The row stopped being ours between storing the handles and
            # settling. Take the child back — it is a credential for a state
            # that no longer exists — and destroy the key. The ref may already
            # have been collected by whoever took the row, in which case the
            # provider answers 404 and ``revoke`` treats that as the success it
            # is; a second revoke is cheap, an unrevoked key is not.
            self._discard_orphan_child(session, child.id, claim.user_id)
            await self._discard_orphan_key(
                session, claim, parent, provisioner, secret, minted.external_ref
            )
            report.skipped.append("claim_lost")
            return
        session.refresh(membership)
        report.provisioned += 1
        await SecurityEventService.create_event(
            session=session,
            user_id=claim.user_id,
            data=SecurityEventCreate(
                event_type=EVENT_MINTED,
                severity="medium",
                details={
                    "managed_credential_id": str(claim.parent_id),
                    "child_credential_id": str(child.id),
                    "target_user_id": str(claim.user_id),
                    "external_key_ref": minted.external_ref,
                },
            ),
        )

    # ------------------------------------------------------------------ #
    # The claim token
    # ------------------------------------------------------------------ #

    def _write_under_claim(
        self, session: Session, claim: "_MintClaim", **values: object
    ) -> "_MintClaim | None":
        """Apply ``values`` only if this attempt still owns the row.

        A conditional ``UPDATE ... WHERE id = ? AND status = 'minting' AND
        updated_at = ?`` rather than a read-then-write: the check and the write
        are then one statement and nothing can slip between them. ``rowcount``
        is the answer — ``0`` means the row was settled, suspended or cascaded
        away while we were inside the provider call.

        Status alone would not be enough, for the same reason the status-repair
        scheduler pairs it with a timestamp: a deactivation followed by a
        reactivation can put a row back through ``minting`` under a *different*
        attempt, and a status-only check would let this one write over it.

        Returns the new claim (the stamp it just wrote) so a caller can chain a
        second conditional write, or ``None`` when the claim is lost.
        """
        stamp = datetime.now(timezone.utc)
        updated = session.execute(
            sa.update(ManagedAICredentialMembership)
            .where(
                col(ManagedAICredentialMembership.id) == claim.membership_id,
                col(ManagedAICredentialMembership.status)
                == MembershipProvisioningStatus.MINTING.value,
                col(ManagedAICredentialMembership.updated_at) == claim.stamp,
            )
            .values(updated_at=stamp, **values)
            # Read the stored stamp back and build the next claim from *that*.
            # The guarantee is simply stated: **a claim stamp is always a value
            # that came out of the column**, so it is always naive and always
            # byte-identical to what the next ``WHERE updated_at = ?`` compares
            # against — the initial claim's stamp is a ``session.refresh`` of
            # this column, and every chained one is this ``RETURNING``.
            #
            # Written down because the round trip looks redundant and is the
            # kind of thing that gets tidied away, and because the reason it
            # *used* to give was wrong: it claimed the aware value we send and
            # the naive column could not compare equal. They do — psycopg2 sends
            # an aware datetime as ``::timestamptz`` and Postgres casts the
            # ``timestamp`` column with the session ``TimeZone``, the same cast
            # that produced the stored value — so chaining on the sent value
            # would have matched. Keep the round trip for the guarantee above,
            # not for a conversion hazard that does not exist.
            .returning(col(ManagedAICredentialMembership.updated_at))
            .execution_options(synchronize_session=False)
        ).first()
        session.commit()
        if updated is None:
            logger.warning(
                "Key provisioning lost its claim on membership %s (user %s) "
                "while minting; the row was settled elsewhere.",
                claim.membership_id, claim.user_id,
            )
            return None
        return claim._replace(stamp=updated[0])

    async def _discard_orphan_key(
        self,
        session: Session,
        claim: "_MintClaim",
        parent: ManagedAICredential,
        provisioner: KeyProvisioner,
        secret: str,
        external_ref: dict,
    ) -> None:
        """Destroy a key we minted and then could not record. Never silent.

        Reached only from a lost claim, and the ordering of its two halves is
        the invariant restated: revoke first, and if the revoke fails write the
        durable event **carrying the external ref**, because at that point the
        ref exists nowhere else — the row that would have held it is gone or has
        been taken over by somebody who never saw this key.
        """
        request = RevocationRequest(
            user_id=claim.user_id,
            parent_id=claim.parent_id,
            provider_admin_credential_id=parent.provider_id,
            provider_type=(
                parent.type.value
                if hasattr(parent.type, "value")
                else str(parent.type)
            ),
            external_key_ref=dict(external_ref or {}),
            # The holder may have been deleted — that is one of the three ways
            # this claim gets lost — so the feed is chosen the same way the
            # deletion path chooses it, and ``None`` means "log it, there is
            # nobody to tell".
            audit_user_id=(
                claim.user_id if self._user_exists(session, claim.user_id) else None
            ),
        )
        try:
            await provisioner.revoke(secret, request.external_key_ref)
        except Exception as exc:
            restore_session(session)
            code = getattr(exc, "code", None) or "revoke_failed"
            await self._emit_revoke_failed(session, request, code)
            return
        await self._emit_revoked(session, request)

    @staticmethod
    def _user_exists(session: Session, user_id: uuid.UUID) -> bool:
        """Fresh existence check, deliberately not ``session.get``.

        ``get`` answers from the identity map, and the whole point of asking
        here is that the row may have been deleted by another transaction since
        we loaded it.
        """
        return (
            session.exec(select(User.id).where(User.id == user_id)).first()
            is not None
        )

    @staticmethod
    def _discard_orphan_child(
        session: Session, child_id: uuid.UUID, owner_id: uuid.UUID
    ) -> None:
        """Delete a child credential whose membership no longer wants it."""
        try:
            ai_credentials_service.delete_credential(
                session, child_id, owner_id, force=True, admin_override=True
            )
        except Exception:  # pragma: no cover - defensive
            restore_session(session)
            logger.exception(
                "Could not discard orphaned minted credential %s for user %s",
                child_id, owner_id,
            )

    # ------------------------------------------------------------------ #
    # Status transitions
    # ------------------------------------------------------------------ #

    def _settle(
        self,
        session: Session,
        membership: ManagedAICredentialMembership,
        *,
        status: MembershipProvisioningStatus,
        error: str | None,
    ) -> None:
        """Write a terminal (or terminal-ish) status and clear the retry state.

        For the statuses that are settled *outside* a mint attempt — i.e. before
        the claim is taken. Nothing an attempt writes comes through here:
        ``provisioned`` and every failure alike happen after the claim, so they
        must be conditional on it and go through :meth:`_write_under_claim`.
        """
        membership.status = status.value
        membership.last_error = error
        membership.next_attempt_at = None
        membership.updated_at = datetime.now(timezone.utc)
        session.add(membership)
        session.commit()
        session.refresh(membership)

    def _record_failure(
        self,
        session: Session,
        claim: "_MintClaim",
        membership: ManagedAICredentialMembership,
        code: str,
    ) -> bool:
        """Back off, or give up for good once the attempt ceiling is reached.

        The ceiling is what makes ``failed`` mean something. Without it a
        permanently broken configuration — a revoked admin secret, an uncapped
        project — would sit at ``pending`` with an ever-later retry, which reads
        as "still working" to every surface that shows it.

        **Conditional on the claim, exactly like the success path**, and this is
        the sharper half of the two: the retry branch writes ``pending``, so an
        unconditional version hands a row back to the converge queue that
        somebody else has since settled. The concrete case is a deactivation
        landing inside the mint ``await``: it writes ``suspended`` — a status
        documented as meaning "this account is disabled and nothing is being
        minted for it" — and an unconditional failure write then puts it back to
        ``pending`` with a retry time. Returns ``False`` when the row stopped
        being this attempt's to write, and the caller reports ``claim_lost``
        rather than a failure that is no longer about anything.

        On a successful write the in-memory row is refreshed, because
        ``_write_under_claim`` commits — which expires the instance — and both
        of the things every caller does next read off it: ``_emit_mint_failed``
        takes ``status`` and ``provision_attempts``, and ``_count_failure``
        takes ``status``.

        **All four call sites emit the event, and that uniformity is the fix
        rather than a tidy-up.** Two of them used to record the failure
        silently, and one of those two — ``child_create_failed`` — can reach the
        attempt ceiling, so a membership could go terminal-``failed`` with no
        security event anywhere and nothing but a log line saying why. The
        ``terminal`` flag on that event is exactly what a reader of the feed is
        looking for.
        """
        if membership.provision_attempts >= self.MAX_ATTEMPTS:
            values: dict[str, object] = {
                "status": MembershipProvisioningStatus.FAILED.value,
                "last_error": code,
                "next_attempt_at": None,
            }
        else:
            index = min(
                membership.provision_attempts - 1, len(self.BACKOFF_SECONDS) - 1
            )
            delay = self.BACKOFF_SECONDS[max(index, 0)]
            values = {
                "status": MembershipProvisioningStatus.PENDING.value,
                "last_error": code,
                "next_attempt_at": datetime.now(timezone.utc)
                + timedelta(seconds=delay),
            }
        if self._write_under_claim(session, claim, **values) is None:
            return False
        session.refresh(membership)
        return True

    @staticmethod
    def _count_failure(
        membership: ManagedAICredentialMembership, report: ConvergeReport
    ) -> None:
        """Tally a recorded failure as terminal or as one more retry to come."""
        if membership.status == MembershipProvisioningStatus.FAILED.value:
            report.failed += 1
        else:
            report.deferred += 1

    async def _emit_mint_failed(
        self,
        session: Session,
        membership: ManagedAICredentialMembership,
        parent: ManagedAICredential,
        code: str,
    ) -> None:
        await SecurityEventService.create_event(
            session=session,
            user_id=membership.user_id,
            data=SecurityEventCreate(
                event_type=EVENT_MINT_FAILED,
                severity="medium",
                details={
                    "managed_credential_id": str(parent.id),
                    "target_user_id": str(membership.user_id),
                    "reason": code,
                    "attempts": membership.provision_attempts,
                    "terminal": membership.status
                    == MembershipProvisioningStatus.FAILED.value,
                },
            ),
        )

    # ------------------------------------------------------------------ #
    # Revocation
    # ------------------------------------------------------------------ #

    async def revoke_now(
        self, session: Session, requests: list[RevocationRequest]
    ) -> int:
        """Destroy the named keys at the provider. Returns how many succeeded.

        Directly awaitable with a session, for the same reason ``converge`` is:
        the fire-and-forget wrapper below is a scheduling detail, not the
        behaviour.
        """
        succeeded = 0
        for request in requests:
            admin_credential = (
                session.get(
                    AIProvider, request.provider_admin_credential_id
                )
                if request.provider_admin_credential_id
                else None
            )
            adapter = registry.find_adapter(request.provider_type)
            provisioner = adapter.key_provisioner if adapter is not None else None
            if admin_credential is None or provisioner is None:
                await self._emit_revoke_failed(
                    session, request, "no_admin_credential"
                )
                continue
            try:
                await provisioner.revoke(
                    ai_providers_service.decrypt_secret(admin_credential),
                    request.external_key_ref,
                )
            except ProviderAdminError as exc:
                await self._emit_revoke_failed(session, request, exc.code)
                continue
            except Exception:
                restore_session(session)
                logger.exception(
                    "Revoking the minted key for user %s failed", request.user_id
                )
                await self._emit_revoke_failed(session, request, "revoke_failed")
                continue
            succeeded += 1
            await self._emit_revoked(session, request)
        return succeeded

    async def _emit_revoked(
        self, session: Session, request: RevocationRequest
    ) -> None:
        await self._audit_revocation(
            session,
            request,
            event_type=EVENT_REVOKED,
            severity="medium",
            extra={},
        )

    async def _emit_revoke_failed(
        self, session: Session, request: RevocationRequest, code: str
    ) -> None:
        """Record a key we could not destroy.

        This event **is** the durable record. The membership row that carried the
        handles is gone by now — that is the delete-then-revoke ordering working
        as intended — so without this the key would be live at the provider with
        nothing anywhere naming it. The external ref goes in deliberately.
        """
        await self._audit_revocation(
            session,
            request,
            event_type=EVENT_REVOKE_FAILED,
            severity="high",
            extra={"reason": code},
        )

    async def _audit_revocation(
        self,
        session: Session,
        request: RevocationRequest,
        *,
        event_type: str,
        severity: str,
        extra: dict,
    ) -> None:
        """Write one revocation event, into whichever feed it belongs in.

        Two things this handles that a direct ``create_event`` call does not:

        **Whose feed.** ``security_event.user_id`` is NOT NULL and has a foreign
        key to ``user``. On the account-deletion path the key holder no longer
        exists by the time the provider is called, so writing to their feed is not
        merely pointless — it is an integrity error that would take the whole
        revoke loop down with it. ``audit_user_id`` names the feed; when it is
        ``None`` there is genuinely nobody to tell, and the log is the record.

        **Best-effort.** An audit row that cannot be written must not be the thing
        that stops the remaining keys in the batch from being destroyed. Repair
        first, then log — on an aborted transaction the logging call is itself a
        query.
        """
        details = {
            "managed_credential_id": str(request.parent_id),
            "target_user_id": str(request.user_id),
            "external_key_ref": request.external_key_ref,
            **extra,
        }
        if request.audit_user_id is None:
            logger.info(
                "%s for user %s (no security feed to record it in): %s",
                event_type, request.user_id, details,
            )
            return
        try:
            await SecurityEventService.create_event(
                session=session,
                user_id=request.audit_user_id,
                data=SecurityEventCreate(
                    event_type=event_type, severity=severity, details=details
                ),
            )
        except Exception:
            restore_session(session)
            logger.exception(
                "Failed to record %s for user %s: %s",
                event_type, request.user_id, details,
            )

    def schedule_revocations(self, requests: list[RevocationRequest]) -> None:
        """Hand a batch of revocations to the background loop.

        Synchronous callers (reconcile, the user-deletion routes) cannot await a
        provider call, and must not: an admin's PATCH should not be as slow as
        the provider is. The coroutine opens its own session because the caller's
        may be committed, rolled back or closed long before this runs.
        """
        if not requests:
            return
        # ``on_drop`` rather than fire-and-forget. The helper's cross-thread
        # scheduling can fail, and its historical answer to that is a warning
        # and ``coro.close()`` — the batch simply does not happen, and neither
        # the log line nor anything else names the keys. That is tolerable for
        # the other callers of a best-effort helper and is not tolerable here:
        # every request in this batch is a key that is live at a provider, and
        # ``_emit_revoke_failed`` is documented as *the* durable record of one.
        create_task_with_error_logging(
            self._revoke_in_new_session(requests),
            "revoke_minted_keys",
            on_drop=lambda: self._record_unscheduled_revocations(requests),
        )

    def _record_unscheduled_revocations(
        self, requests: list[RevocationRequest]
    ) -> None:
        """Write the durable record for keys nothing will now revoke.

        Synchronous, and opens its own session, because the caller has none it
        can rely on and there is no loop to await one on — the reason we are here
        at all. Best-effort like every other audit write on this path: what must
        not happen is the external ref existing nowhere.
        """
        for request in requests:
            logger.error(
                "Could not schedule the revocation of a minted key for user "
                "%s; it is still live at the provider: %s",
                request.user_id, request.external_key_ref,
            )
        try:
            with create_session() as session:
                for request in requests:
                    self._write_revoke_event_sync(
                        session, request, EVENT_REVOKE_FAILED, "not_scheduled"
                    )
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to record unscheduled revocations for %d minted key(s)",
                len(requests),
            )

    async def _revoke_in_new_session(
        self, requests: list[RevocationRequest]
    ) -> None:
        with create_session() as session:
            await self.revoke_now(session, requests)

    # ------------------------------------------------------------------ #
    # Account lifecycle
    # ------------------------------------------------------------------ #

    def collect_user_revocations(
        self,
        session: Session,
        user_id: uuid.UUID,
        *,
        audit_user_id: uuid.UUID | None,
    ) -> list[RevocationRequest]:
        """Every minted key this user holds, as revocation requests.

        Read **before** the rows are deleted. Once the user row goes, the
        membership rows go with it (``ON DELETE CASCADE``) and the handles are
        unrecoverable — a key nobody can name is a key nobody can destroy.
        """
        rows = session.exec(
            select(
                ManagedAICredentialMembership, ManagedAICredential
            ).join(
                ManagedAICredential,
                col(ManagedAICredential.id)
                == col(ManagedAICredentialMembership.managed_credential_id),
            ).where(
                ManagedAICredentialMembership.user_id == user_id,
                holds_provider_key_clause(),
            )
        ).all()
        return [
            RevocationRequest(
                user_id=user_id,
                parent_id=parent.id,
                provider_admin_credential_id=parent.provider_id,
                provider_type=(
                    parent.type.value
                    if hasattr(parent.type, "value")
                    else str(parent.type)
                ),
                external_key_ref=dict(membership.external_key_ref or {}),
                audit_user_id=audit_user_id,
            )
            for membership, parent in rows
            if holds_provider_key(membership)
        ]

    def suspend_user_memberships(
        self,
        session: Session,
        user_id: uuid.UUID,
        revocations: list[RevocationRequest],
    ) -> None:
        """Deactivation: drop this user's minted keys, keep their memberships.

        **``revocations`` is the caller's list, appended to as each row is
        committed, and that is the whole point of it being a parameter rather
        than a return value.** This method commits per row and erases
        ``external_key_ref`` / ``ai_credential_id`` as it goes, so by member
        three members one and two have already had the only record of their
        provider keys deleted. A return value is lost the moment anything
        raises, and the caller (``on_account_deactivated``) is a deliberate
        never-fail net that swallows exactly that — so the two together erased
        the refs and discarded the revocations, leaving keys live at a provider
        with nothing naming them. That is the one outcome this file exists to
        prevent, and it was the never-fail net that caused it. Handing the sink
        in makes the partial result survive the failure: whatever was collected
        before the raise is still the caller's to schedule.

        The membership survives, as ``suspended``, because the person is still a
        member of the record — reactivating them mints a fresh key rather than
        requiring an admin to remember who they used to be. It is deliberately
        not left ``pending``: a pending row on a disabled account would read as
        work in progress for as long as the account stays disabled.

        **Delete the child first, revoke second.** The delete can be refused by
        the Tier-2 blast-radius gate (the credential is a published bundle's
        publisher credential), and a revoke that ran first would leave a dead key
        on a surviving row that reads as healthy everywhere and fails at first
        use. When the delete is blocked the member keeps a working key, which is
        recoverable, and the block is recorded.

        **Which members hold a key is not a status question.** It is
        :func:`holds_provider_key`, the same predicate the deletion path and the
        admin-secret usage count call. This function used to skip ``failed``
        rows on the premise that a terminal failure holds nothing — and the
        premise is false, because neither ``_settle`` nor ``_record_failure``
        clears the ref and two paths reach the ceiling with it populated. The
        result was deactivation and deletion answering the same question
        differently, so deactivating an account could leave a live key behind.

        A ``failed`` row therefore has its key revoked like any other and
        **keeps its status, its error and its attempt count**. That part of the
        old behaviour was right and is deliberate: the failure is a durable
        record an administrator still has to act on, and overwriting it during an
        account action that has nothing to do with it would hand the row back to
        the queue at zero attempts with the reason it failed erased.
        """
        members = session.exec(
            select(
                ManagedAICredentialMembership, ManagedAICredential
            ).join(
                ManagedAICredential,
                col(ManagedAICredential.id)
                == col(ManagedAICredentialMembership.managed_credential_id),
            ).where(
                ManagedAICredentialMembership.user_id == user_id,
            )
        ).all()

        for membership, parent in members:
            if not resolve_policy(session, parent).is_minted:
                # A shared key is one key held by many people. Deactivating one
                # holder must not destroy it for the others; their child row
                # simply stops being reachable with the account.
                continue
            blocked = self.suspend_one_membership(
                session, membership, parent, revocations
            )
            if blocked is not None:
                self._emit_revoke_blocked_sync(
                    session, membership.user_id, parent.id, blocked
                )

    def suspend_one_membership(
        self,
        session: Session,
        membership: ManagedAICredentialMembership,
        parent: ManagedAICredential,
        revocations: list[RevocationRequest],
    ) -> str | None:
        """Drop one membership's key: delete the child, then revoke.

        Returns ``None`` on success, or a block reason (``in_use_bundle``,
        ``delete_failed``) when the child could not be removed and the key was
        therefore left live. The **caller** turns that into whatever it owes its
        own audience — a `revoke_blocked` event for the deactivation cascade, a
        409 for the per-key rotate — because the two answer to different people.

        Everything :meth:`suspend_user_memberships` promises about ordering is
        implemented here, and is why rotate is built from this rather than from
        its own revoke: delete the child first (the delete can be refused by the
        Tier-2 gate, and a revoke that ran first would leave a dead key on a
        surviving row that reads as healthy and fails at first use), build the
        revocation request *before* the row is rewritten, and append it only
        *after* the commit that erases the ref.

        Callers must be minted-only. A shared membership has no key of its own
        and passing one here would destroy the key its co-holders are using; the
        two callers both check ``is_minted`` first, and neither may stop.
        """
        user_id = membership.user_id
        ref = dict(membership.external_key_ref or {})
        child_id = membership.ai_credential_id
        if child_id is not None:
            try:
                ai_credentials_service.delete_credential(
                    session, child_id, user_id,
                    force=False, admin_override=True,
                )
            except AICredentialInUseError:
                logger.warning(
                    "Minted credential %s for user %s is in use by a published "
                    "bundle; leaving the key live.",
                    child_id, user_id,
                )
                return "in_use_bundle"
            except Exception:
                restore_session(session)
                logger.exception(
                    "Could not delete minted credential %s for user %s; "
                    "leaving the key live.", child_id, user_id,
                )
                return "delete_failed"

        # Built before the row is rewritten (the ref is about to be erased)
        # and *appended after the commit* — the two halves are deliberately
        # not adjacent. A request added before the commit that then fails
        # would destroy a key the surviving row still names and still points
        # a live child at, which is the "dead key that reads as healthy"
        # the delete-then-revoke ordering exists to avoid.
        pending_revocation = (
            RevocationRequest(
                user_id=user_id,
                parent_id=parent.id,
                provider_admin_credential_id=parent.provider_id,
                provider_type=(
                    parent.type.value
                    if hasattr(parent.type, "value")
                    else str(parent.type)
                ),
                external_key_ref=ref,
                audit_user_id=user_id,
            )
            if holds_provider_key(membership)
            else None
        )
        membership.ai_credential_id = None
        membership.external_key_ref = None
        if membership.status != MembershipProvisioningStatus.FAILED.value:
            membership.status = MembershipProvisioningStatus.SUSPENDED.value
            membership.provision_attempts = 0
            membership.next_attempt_at = None
            membership.last_error = None
        # ``updated_at`` moves in every branch, ``failed`` included. It is
        # not decoration: it is half of the mint claim token, so touching the
        # row here is what tells a converge pass that is inside a provider
        # call right now that the row it claimed is no longer its to write.
        membership.updated_at = datetime.now(timezone.utc)
        session.add(membership)
        session.commit()
        if pending_revocation is not None:
            revocations.append(pending_revocation)
        return None

    def rotate_member_key(
        self,
        session: Session,
        membership: ManagedAICredentialMembership,
        parent: ManagedAICredential,
    ) -> ManagedAICredentialMembership:
        """Destroy one member's key and queue a fresh mint.

        **Built from the suspend/resume pair, not from its own revoke.**
        Deactivating an account already destroys a minted key and reactivating
        it already mints a new one; that sequence carries the delete-then-revoke
        ordering, the blast-radius gate, the claim-token touch and the "never
        leave a key live and unrecorded" guarantee, and it is tested. A bespoke
        rotate would be a second implementation of the one sequence that must
        never get this wrong.

        The resume half is written here rather than delegated to
        :meth:`resume_user_memberships`, which resumes *every* suspended
        membership this person has — rotating one key would silently requeue the
        keys they hold under every other record. It is also why the row goes
        straight to ``pending`` even when it was ``failed``: a rotate is an
        administrator saying "try again with a new key", which is exactly what
        the retry verb means, and leaving it ``suspended`` would strand a row
        that nothing converges.

        The holder is **without a key until the next converge tick**. That
        window is real, is at most one minute, and is the same one a
        deactivation followed by a reactivation already produces; the surface
        says so rather than pretending the new key is instant.

        Raises 400 for a shared membership (rotating a pasted key is the
        provider's *Replace key*), 409 while a mint is in flight, and 409 when
        the child credential cannot be deleted.
        """
        if not resolve_policy(session, parent).is_minted:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This key is shared: one key held by every member. Replace "
                    "it on the provider instead — rotating it here would give "
                    "one member a key nobody else has."
                ),
            )
        if membership.status == MembershipProvisioningStatus.MINTING.value:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A key is being created for this member right now. Try "
                    "again in a moment."
                ),
            )
        revocations: list[RevocationRequest] = []
        blocked = self.suspend_one_membership(
            session, membership, parent, revocations
        )
        if blocked is not None:
            self._emit_revoke_blocked_sync(
                session, membership.user_id, parent.id, blocked
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "This member's credential is in use by a published bundle "
                    "and could not be replaced; their key was left working."
                    if blocked == "in_use_bundle"
                    else "Could not remove this member's credential; their key "
                    "was left working."
                ),
            )
        membership.status = MembershipProvisioningStatus.PENDING.value
        membership.provision_attempts = 0
        membership.next_attempt_at = None
        membership.last_error = None
        membership.updated_at = datetime.now(timezone.utc)
        session.add(membership)
        session.commit()
        session.refresh(membership)
        # After the row is committed, never before: a revoke that ran first
        # would destroy a key for a rotation the database then refused.
        if revocations:
            self.schedule_revocations(revocations)
        return membership

    def requeue_failed_member(
        self,
        session: Session,
        *,
        parent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ManagedAICredentialMembership:
        """Put a terminally ``failed`` member back in the mint queue.

        **The way out of ``failed``, and there has to be one.** The status is
        deliberately terminal — bounded retries that converge are the whole point
        — but terminal must not mean unreachable. The likeliest first-run failure
        is a project whose spend limit is not enforcing; the admin fixes it in a
        minute, and without this they would have no way to say "try again" that
        does anything.

        Deliberately **not** folded into ``add_members``. Re-adding an existing
        member is a no-op there, and making it a requeue instead would mean any
        PATCH that merely renames the record — reconcile passes the current
        membership list as the desired one — quietly resets every durable failure
        it touches. A retry has to be a thing an administrator asks for.
        """
        membership = session.exec(
            select(ManagedAICredentialMembership).where(
                ManagedAICredentialMembership.managed_credential_id == parent_id,
                ManagedAICredentialMembership.user_id == user_id,
            )
        ).first()
        if membership is None:
            raise HTTPException(
                status_code=404,
                detail="That user is not a member of this credential.",
            )
        if membership.status != MembershipProvisioningStatus.FAILED.value:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only a member whose key provisioning has failed can be "
                    f"retried; this one is '{membership.status}'."
                ),
            )
        membership.status = MembershipProvisioningStatus.PENDING.value
        membership.provision_attempts = 0
        membership.next_attempt_at = None
        # ``last_error`` is deliberately kept: until the retry succeeds, why it
        # failed last time is still the most useful thing anyone can read here,
        # and clearing it would make a row that is about to fail the same way
        # look like a row that has never been tried.
        membership.updated_at = datetime.now(timezone.utc)
        session.add(membership)
        session.commit()
        session.refresh(membership)
        return membership

    # ------------------------------------------------------------------ #
    # Owner-facing state
    # ------------------------------------------------------------------ #

    #: Statuses that mean "this person is a member and holds no key". The one
    #: list, used by both owner-facing readers below, so "is a key on the way"
    #: and "what should the dashboard show" can never answer differently.
    KEYLESS_STATUSES = (
        MembershipProvisioningStatus.PENDING.value,
        MembershipProvisioningStatus.MINTING.value,
        MembershipProvisioningStatus.FAILED.value,
    )
    #: The subset of the above that is still moving. ``failed`` is terminal and
    #: is *not* in flight — a screen that treated it as such would spin forever.
    IN_FLIGHT_STATUSES = (
        MembershipProvisioningStatus.PENDING.value,
        MembershipProvisioningStatus.MINTING.value,
    )

    def list_user_provisionings(
        self, session: Session, user_id: uuid.UUID
    ) -> list[UserKeyProvisioningPublic]:
        """This user's memberships that have no key behind them yet.

        ``suspended`` is deliberately absent: it only exists on a deactivated
        account, and a deactivated account has nobody looking at this screen.
        """
        rows = session.exec(
            select(ManagedAICredentialMembership, ManagedAICredential)
            .join(
                ManagedAICredential,
                col(ManagedAICredential.id)
                == col(ManagedAICredentialMembership.managed_credential_id),
            )
            .where(
                ManagedAICredentialMembership.user_id == user_id,
                col(ManagedAICredentialMembership.status).in_(
                    self.KEYLESS_STATUSES
                ),
            )
        ).all()
        return [
            UserKeyProvisioningPublic(
                managed_credential_id=parent.id,
                name=parent.name,
                type=parent.type.value
                if hasattr(parent.type, "value")
                else str(parent.type),
                status=MembershipProvisioningStatus(membership.status),
                last_error=membership.last_error,
                updated_at=membership.updated_at,
            )
            for membership, parent in rows
        ]

    def api_key_onboarding_state(
        self, session: Session, user_id: uuid.UUID
    ) -> AIKeyOnboardingState:
        """Should this account be asked to paste an API key?

        **The whole answer, taken here.** See :class:`AIKeyOnboardingState` for
        why it is a server field and not two booleans the browser combines.
        """
        return self.api_key_onboarding_states(session, [user_id]).get(
            user_id, AIKeyOnboardingState.NEEDS_KEY
        )

    def api_key_onboarding_states(
        self, session: Session, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, AIKeyOnboardingState]:
        """The same answer for a list of people, in two queries.

        The batched form is the real implementation and the single-user one
        delegates to it, rather than the other way round: the admin surfaces ask
        this for every member of a record, and a per-user implementation there is
        an N+1 on a projection that already has a query-count test.

        Both halves are provider-agnostic, and that is the change this function
        carries. "Does the wall have anything to ask for" is
        ``owner_ids_with_a_default`` — an environment needs a default credential
        of the type *its* SDK expects and nothing in that code names a provider,
        so scoping this to Anthropic made ``preparing → has_key`` unreachable:
        every mint this platform can actually perform is a non-Anthropic one.
        "Would the wall be pointless right now" was already provider-agnostic;
        the two halves now agree instead of disagreeing by one provider.
        """
        ids = list(user_ids)
        if not ids:
            return {}
        with_key = ai_credentials_service.owner_ids_with_a_default(session, ids)
        in_flight = set(
            session.exec(
                select(ManagedAICredentialMembership.user_id).where(
                    col(ManagedAICredentialMembership.user_id).in_(ids),
                    col(ManagedAICredentialMembership.status).in_(
                        self.IN_FLIGHT_STATUSES
                    ),
                )
            ).all()
        )
        states: dict[uuid.UUID, AIKeyOnboardingState] = {}
        for user_id in ids:
            if user_id in with_key:
                states[user_id] = AIKeyOnboardingState.HAS_KEY
            elif user_id in in_flight:
                states[user_id] = AIKeyOnboardingState.PREPARING
            else:
                states[user_id] = AIKeyOnboardingState.NEEDS_KEY
        return states

    def resume_user_memberships(
        self, session: Session, user_id: uuid.UUID
    ) -> int:
        """Reactivation: put suspended memberships back in the mint queue."""
        suspended = session.exec(
            select(ManagedAICredentialMembership).where(
                ManagedAICredentialMembership.user_id == user_id,
                ManagedAICredentialMembership.status
                == MembershipProvisioningStatus.SUSPENDED.value,
            )
        ).all()
        for membership in suspended:
            membership.status = MembershipProvisioningStatus.PENDING.value
            membership.provision_attempts = 0
            membership.next_attempt_at = None
            membership.last_error = None
            membership.updated_at = datetime.now(timezone.utc)
            session.add(membership)
        if suspended:
            session.commit()
        return len(suspended)

    def _emit_revoke_blocked_sync(
        self,
        session: Session,
        user_id: uuid.UUID,
        parent_id: uuid.UUID,
        reason: str,
    ) -> None:
        """Record a key deliberately left live, from synchronous code."""
        self._write_security_event_sync(
            session,
            user_id=user_id,
            event_type=EVENT_REVOKE_BLOCKED,
            details={
                "managed_credential_id": str(parent_id),
                "target_user_id": str(user_id),
                "reason": reason,
            },
        )

    def _write_revoke_event_sync(
        self,
        session: Session,
        request: RevocationRequest,
        event_type: str,
        reason: str,
    ) -> None:
        """The synchronous counterpart of :meth:`_audit_revocation`.

        Same feed rule (``audit_user_id`` names it; ``None`` means there is
        nobody to tell and the log is the record) and the same details, external
        ref included — the ref is the whole reason the event exists.
        """
        details = {
            "managed_credential_id": str(request.parent_id),
            "target_user_id": str(request.user_id),
            "external_key_ref": request.external_key_ref,
            "reason": reason,
        }
        if request.audit_user_id is None:
            logger.error(
                "%s for user %s (no security feed to record it in): %s",
                event_type, request.user_id, details,
            )
            return
        self._write_security_event_sync(
            session,
            user_id=request.audit_user_id,
            event_type=event_type,
            details=details,
        )

    def _write_security_event_sync(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        event_type: str,
        details: dict,
    ) -> None:
        """Write one security event from synchronous code. Best-effort.

        Rows constructed directly rather than through the ``async``
        ``SecurityEventService.create_event``: these paths are reached from
        synchronous route handlers with no loop to schedule on, exactly as
        ``AccountProvisioningService`` documents for the same reason. An audit
        row that cannot be written must not break a deactivation.
        """
        import json

        from app.models.events.security_event import SecurityEvent

        try:
            session.add(
                SecurityEvent(
                    user_id=user_id,
                    event_type=event_type,
                    severity="high",
                    details=json.dumps(details),
                )
            )
            session.commit()
        except Exception:  # pragma: no cover - defensive
            restore_session(session)
            logger.exception(
                "Failed to record %s for user %s", event_type, user_id
            )


key_provisioning_service = KeyProvisioningService()


def schedule_revocations(requests: list[RevocationRequest]) -> None:
    """Module-level alias so callers need not import the singleton by name."""
    key_provisioning_service.schedule_revocations(requests)


__all__ = [
    "ConvergeReport",
    "KeyProvisioningService",
    "key_provisioning_service",
    "schedule_revocations",
    "EVENT_MINTED",
    "EVENT_MINT_FAILED",
    "EVENT_REVOKED",
    "EVENT_REVOKE_FAILED",
    "EVENT_REVOKE_BLOCKED",
]
