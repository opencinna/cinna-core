"""InvitationService — the whole lifecycle of "you have been invited".

WHAT THIS IS
------------
Zero-touch onboarding phase 3. An administrator invites a person by address:
the account row is created through the ordinary chokepoint
(``UserService.create_account(origin=AccountOrigin.INVITE)``) with no password,
the managed AI credentials the admin chose are granted through
``AccountProvisioningService``, and one ``user_invitation`` row records the
outstanding offer plus the ``jti`` of the single live token that redeems it.

The person accepts by setting a password, by signing in with Google (the
existing auto-link by address does the work; this service only marks the row),
or by completing a password reset — all three end at
:meth:`mark_accepted_if_pending` or :meth:`accept_with_password`.

FOUR RULES THIS FILE EXISTS TO KEEP IN ONE PLACE
------------------------------------------------
**1. The token is minted from the committed row.** :meth:`create_invitation`
returns ``(row, token)`` and mints the token *after* the commit, from the value
the database actually holds. A token minted from a ``jti`` that never lands —
or that a later rotation supersedes before the commit — passes signature and
purpose verification and then fails the row lookup, which is by design
indistinguishable from a forgery. Every recipient would see "this invitation is
no longer valid" and no log would say why.

**2. Verification resolves by ``jti``, never by address.** ``token_jti`` is
what resend rotates, so resolving by ``sub`` would make rotation invalidate
nothing: a revoked-then-resent invitation's *old* link would still work. The
address is then compared against the resolved user's own as an **independent
second check**, which is what makes an admin's email change invalidate the
outstanding link.

**3. Status is derived in exactly one function.** :meth:`status_of` and nowhere
else. The admin projection, the public lookup and the accept path all call it
and compare against the constants in ``models/users/user_invitation.py``. The
first time a second implementation of the accepted → revoked → expired →
pending ordering appears, it will eventually disagree with this one, and the
way that surfaces is a revoked invitation that is still acceptable.

**4. "May this person use a password" is answered once.**
:meth:`password_accepted` delegates to
``AccessPolicyService.is_password_auth_allowed`` — the same call
``require_password_auth`` is built on — and the answer is surfaced on the
lookup projection as a single derived boolean. The accept path enforces the
identical call. The client renders the password form on that boolean and
nothing else. Two predicates for one question is how a non-superuser on a
Google-only instance ends up looking at a form whose submission returns a
deliberately detail-free 400.

NON-ENUMERATION
---------------
Every public failure is the same failure. A forged token, a cross-purpose
token, a rotated-away ``jti``, an expired / revoked / already-accepted
invitation, a deleted or deactivated user, a changed address, and "password
auth is not available to you" all produce ``valid=false`` from
:meth:`lookup` and the same :class:`InvitationInvalidError` from
:meth:`accept_with_password`. There is no branch a caller can distinguish, and
in particular there is no 403: a status that appeared only for a *real* token
would answer "is this token genuine", which is the one question this surface
must not answer.

The residual difference is timing — the first three cases return before any
database access, the rest do one indexed SELECT. That separates "forged" from
"server-signed", which is not a secret: anyone who can produce a server-signed
token already holds one. A constant-time floor here would buy nothing and cost
a request-path delay, so there deliberately is not one.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.events.security_event import SecurityEventCreate
from app.models.users.user import (
    VALID_USER_ROLES,
    AccountOrigin,
    User,
    UserRole,
)
from app.models.users.user_invitation import (
    INVITATION_AUTH_HINT_ANY,
    INVITATION_AUTH_HINT_GOOGLE,
    INVITATION_AUTH_HINT_PASSWORD,
    INVITATION_STATUS_ACCEPTED,
    INVITATION_STATUS_EXPIRED,
    INVITATION_STATUS_PENDING,
    INVITATION_STATUS_REVOKED,
    InviteProvisioningSkip,
    InviteProvisioningSummary,
    InviteUserRequest,
    InvitationLookupPublic,
    UserInvitation,
    UserInvitationPublic,
)
from app.services.events.security_event_service import SecurityEventService
from app.services.users.access_policy_service import AccessPolicy, AccessPolicyService
from app.services.users.account_provisioning_service import (
    AccountProvisioningService,
)
from app.services.users.email_confirmation_service import EmailConfirmationService
from app.services.users.user_service import UserService
from app.utils import (
    as_utc,
    generate_invitation_email,
    generate_invitation_token,
    restore_session,
    send_email,
    verify_invitation_token,
)

logger = logging.getLogger(__name__)


# Audit event types. One namespace, one per lifecycle transition, so an
# administrator reading a person's security feed sees the whole story of how
# that account came to exist. Every one of these is written into the *invited
# account's* feed, and each is written exactly once.
EVENT_INVITATION_CREATED = "user.invitation.created"
EVENT_INVITATION_RESENT = "user.invitation.resent"
EVENT_INVITATION_REVOKED = "user.invitation.revoked"
EVENT_INVITATION_ACCEPTED = "user.invitation.accepted"
EVENT_INVITATION_LINK_ISSUED = "user.invitation.link_issued"

# The one event written into the *acting administrator's* own feed, and the
# only reason it needs a type of its own.
#
# An administrative act on someone else's behalf produces two rows in two
# feeds, and they are two different facts: "an account was created for you"
# and "you created an account for someone". ``admin_llm_providers`` writes the
# same pair — per-child ``admin.ai_credential.provision`` keyed to the owner,
# and a distinct parent ``admin.managed_ai_credential.create`` keyed to the
# admin — and the distinct type is the whole point of that shape, not
# decoration. Emitting ``user.invitation.created`` into both feeds instead
# records one fact twice: an admin onboarding fifty people accumulates fifty
# rows in their own security feed that read as if fifty accounts had been
# created *for them*, and no consumer can tell those from the one row that
# genuinely was.
#
# Named for the object the admin acted on, in the ``admin.`` namespace, with
# the imperative verb — the same three parts, in the same order, as
# ``admin.managed_ai_credential.create``.
EVENT_ADMIN_INVITATION_CREATED = "admin.user_invitation.create"


# ── Errors ─────────────────────────────────────────────────────────────


class InvitationError(Exception):
    """Base for the invitation domain errors the routes map to responses."""


class InvitationInvalidError(InvitationError):
    """The public, deliberately uninformative failure.

    One exception type for every reason a token does not redeem, because the
    route must answer identically for all of them. If a caller ever needs to
    know *which* reason, that is a request to build an enumeration oracle and
    the answer is no — log it server-side instead.
    """

    def __init__(self) -> None:
        super().__init__("This invitation is no longer valid")


class InvitationNotFoundError(InvitationError):
    """No invitation exists for this account. Superuser context only."""


class InvitationAlreadyAcceptedError(InvitationError):
    """The invitation has been accepted, so there is nothing left to change.

    The one genuinely terminal state: an accepted invitation cannot be
    resent or revoked, because the account it created is now in use and
    neither action would do anything to it. Every *other* state is a
    transition this service allows — resending an expired invitation is
    exactly what resend is for, and revoking an already-revoked one is a
    no-op-shaped success.
    """


class InvitationNotPendingError(InvitationError):
    """The invitation exists but is not ``pending``, so there is no live link.

    Distinct from :class:`InvitationAlreadyAcceptedError` because it also
    covers revoked and expired, and because the message names the status —
    superuser context, where being specific is the point. Carries the status
    so a caller can render it without deriving it a second time.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(
            f"This invitation is {status}; send a new invitation to issue a "
            "working link."
        )


class InvitationCooldownError(InvitationError):
    """Resend was asked for again too soon. Carries when it will be allowed.

    Per row, off this invitation's own ``last_sent_at`` — not a global
    throttle. The thing being protected is one person's inbox.
    """

    def __init__(self, available_at: datetime) -> None:
        self.available_at = available_at
        super().__init__("An invitation email was sent to this address recently")


# ── Results ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InviteResult:
    """Everything :meth:`InvitationService.invite` produced.

    ``user`` is the ORM row, not a ``UserPublic``: by the time this returns
    there have been at least four commits (the account row, its provisioned
    children, the invitation, the audit events), so the instance is expired and
    ``model_dump()`` — which reads ``__dict__`` and does not emit a SELECT —
    would hand back a projection missing ``id`` and ``email``. The route builds
    the projection through ``api/routes/_user_public.py::user_to_public``,
    which repairs that first. Building it here would also mean a service
    importing a route module.
    """

    user: User
    invitation: UserInvitationPublic
    accept_url: str
    email_sent: bool
    provisioning: InviteProvisioningSummary
    # Whether this invite re-used a row that already existed rather than
    # creating one. See :class:`InviteUserResponse`, which is where it is
    # disclosed to the admin; the audit event carries it too.
    adopted_existing_account: bool = False


@dataclass(frozen=True)
class ResendResult:
    """A rotated invitation and the link that now redeems it."""

    invitation: UserInvitationPublic
    accept_url: str
    email_sent: bool


class InvitationService:
    """The invitation lifecycle. See the module docstring for the four rules."""

    # ── Pure derivations ───────────────────────────────────────────────

    @staticmethod
    def status_of(invitation: UserInvitation) -> str:
        """The one place an invitation's status is derived.

        Ordering is fixed and matters: an invitation that was revoked and has
        since expired reads ``revoked``, because that is the fact an admin
        acted on. Accepted outranks both — once the account is in use, how the
        offer would otherwise have ended is history.
        """
        if invitation.accepted_at is not None:
            return INVITATION_STATUS_ACCEPTED
        if invitation.revoked_at is not None:
            return INVITATION_STATUS_REVOKED
        if as_utc(invitation.expires_at) <= datetime.now(UTC):
            return INVITATION_STATUS_EXPIRED
        return INVITATION_STATUS_PENDING

    @staticmethod
    def password_accepted(policy: AccessPolicy, user: User) -> bool:
        """Whether ``user`` may accept their invitation with a password.

        Delegates, deliberately, rather than restating
        ``policy.password_auth_enabled or user.is_superuser``. That predicate
        already exists and already carries the superuser break-glass reasoning;
        a copy here would be the second implementation of a policy question,
        and the pair would drift the first time the rule changes.
        """
        return AccessPolicyService.is_password_auth_allowed(policy, user)

    @staticmethod
    def effective_auth_hint(
        policy: AccessPolicy, user: User, invitation: UserInvitation
    ) -> str:
        """Which sign-in method the *email* should lead with, right now.

        Resolved at send time and never stored, which is the point: an
        invitation created when passwords were allowed and resent after the
        instance was switched to Google-only must not still say "choose a
        password".

        Presentational only. It narrows to the method that is actually
        available when only one is, and otherwise honours the admin's stored
        preference. It never *enables* anything —
        :meth:`password_accepted` is the only answer to that, and this
        function is built on it rather than beside it.
        """
        if not InvitationService.password_accepted(policy, user):
            # Narrowing to Google is only honest while Google is actually on.
            # The both-off state is REACHABLE, so this is a real branch and
            # not a paranoid one: phase 1's lockout validation guards the
            # transition into Google-only, and ``google_auth_enabled`` is
            # derived from the environment rather than from the row, so an
            # instance can lose its Google client id afterwards.
            #
            # ``any`` is the honest projection — the lookup's client renders
            # on ``password_accepted`` and ``google_auth_enabled``, never on
            # this hint (rule 4). The *email* has no "neither" branch and must
            # not be sent at all in that state, which is :meth:`send`'s job,
            # not this function's.
            if not policy.google_auth_enabled:
                return INVITATION_AUTH_HINT_ANY
            return INVITATION_AUTH_HINT_GOOGLE
        if not policy.google_auth_enabled:
            return INVITATION_AUTH_HINT_PASSWORD
        return invitation.auth_hint

    @staticmethod
    def build_accept_url(token: str) -> str:
        """The link that goes in the email and comes back to the admin."""
        return f"{settings.FRONTEND_HOST}/accept-invite?token={token}"

    @staticmethod
    def mask_email(email: str) -> str:
        """``alice@acme.com`` → ``a***@acme.com``.

        Shown only behind a valid token, so the holder already knows the
        address; the masking is so that a link forwarded into a group chat does
        not restate it in full. Never reveals more than the first character and
        the domain, and a one-character local part is masked to ``***@…``
        rather than to itself.
        """
        local, separator, domain = email.partition("@")
        if not separator:
            return "***"
        if len(local) <= 1:
            return f"***@{domain}"
        return f"{local[0]}***@{domain}"

    # ── Reads ──────────────────────────────────────────────────────────

    @staticmethod
    def get_for_user(
        session: Session, user_id: uuid.UUID
    ) -> UserInvitation | None:
        """This account's invitation, or ``None``. Unique by construction."""
        return session.exec(
            select(UserInvitation).where(UserInvitation.user_id == user_id)
        ).first()

    @staticmethod
    def claim_refused(session: Session, user: User) -> bool:
        """Whether this account may still be **claimed**. One answer, every door.

        A never-claimed invited account can be handed a way in by more than
        one door, and each door that can do it asks exactly this question.
        There are three, and all three are consumers of this function:

        * ``POST /password-recovery/`` — refusal is the same silent 200 it
          gives an address with no account at all;
        * ``POST /reset-password/`` — refusal is the same ``Invalid token``
          it gives a forged token;
        * the Google auto-link inside :meth:`AuthService.link_google_account`
          — refusal is the same generic registration refusal a stranger gets.

        The invite link is the fourth way in and deliberately does *not* ask
        here: :meth:`_resolve` already refuses anything :meth:`status_of` does
        not read ``pending``, and it does so from the token, before it has an
        account in hand. Same rule, asked one step earlier.

        ``POST /auth/google/link`` — an already-signed-in person attaching a
        Google identity — reaches this function too, through the third bullet,
        and so is a fifth *caller*. It is never a fifth door: it refuses
        anyone who already has a ``google_id``, so everyone who reaches the
        gate there authenticated with a password and fails
        :meth:`is_unclaimed` below without issuing a query. That inertness is
        load-bearing, and the argument for it lives at
        ``AuthService.link_google_account_for_user`` as well as here.

        THE HOLE THIS CLOSES
        --------------------
        An invited account is a real, active row with ``hashed_password=None``
        and ``google_id=None`` from the moment the invitation is created.
        Revoking the invitation stops the *invite* link working and nothing
        else, so without this the person the admin just un-invited types their
        address into "forgot password", receives a genuine reset link, sets a
        password and is in — or simply presses "Sign in with Google" on the
        same address and is in without even that. The admin's list still says
        ``revoked``. Expired is worse than revoked, because nothing
        deactivates an account when an offer lapses: the invitation goes stale
        after ``INVITATION_EXPIRE_DAYS`` and the account stays claimable forever.

        Revocation has to mean the account cannot be claimed, not merely that
        one link stopped working — and "cannot be claimed" is only true if
        every door asks.

        THE GATE, AND WHY IT IS :meth:`is_unclaimed`
        --------------------------------------------
        This only ever describes an account that has **never been claimed**,
        and "claimed" is not this function's to define: :meth:`is_unclaimed`
        is the one answer to it, and it means *neither a password hash nor a
        linked Google identity* — the two, and only two, ways an account
        acquires a way in. Asking the narrower ``hashed_password is None``
        here would be a second implementation of one question, and it drifted
        exactly the way a second implementation always does (see below).

        A claimed account is checked first and is permanently exempt: the
        moment someone accepts, their invitation reads ``accepted`` forever,
        and an accepted invitation must never take password recovery — or a
        Google re-link — away from an account in daily use. That ordering is
        what makes the rule expire on its own.

        WHY A GOOGLE-ACCEPTED INVITEE GETS EMAILED RECOVERY
        --------------------------------------------------
        An invitation accepted through Google leaves the row ``accepted`` with
        ``hashed_password`` still ``None``. That account is *claimed* — a
        person proved control of the address to Google and has been signing in
        with it — so there is no un-invited stranger to keep out, which is the
        only thing this predicate exists to do. Gating on the password column
        alone refused them: they read ``accepted`` in the admin list, asked for
        a reset, and silently got nothing, with no way to learn that
        ``POST /users/me/set-password`` while signed in was the one supported
        route to a password. Adding a password to a Google account by emailed
        link is a legitimate thing to want, the link goes to an address Google
        already vouched for, and refusing it protected nobody.

        THE OTHER IMPLEMENTATION OF THIS RULE, AND WHY IT IS NOT USED
        -------------------------------------------------------------
        A second design answers the same question by making revocation
        deactivate the account (``user.is_active = False`` in :meth:`revoke`),
        after which the ``not user.is_active`` checks that already exist on
        every login path do all the work and this predicate is deleted.

        It was rejected, and the reason is the second half of the hole above:
        **it does not close the expired case at all**. Nothing deactivates an
        account when an offer lapses — there is no sweeper — so an expired
        invitation would stay claimable through every door. It also couples
        revocation to ``is_active``, a control admins toggle for entirely
        unrelated reasons, so re-activating a suspended account would silently
        un-revoke an invitation. One predicate at three doors closes both
        cases and stays orthogonal to the activation switch.

        FAILS CLOSED, AND WHY THAT IS FREE
        ----------------------------------
        Never raises. Every caller answers a refusal with something already
        indistinguishable from its ordinary output (the three bullets above),
        so refusing on an unexpected error leaks nothing — while *allowing* on
        one would re-open the bypass at all three doors for exactly as long as
        the database is unwell. The cost is that a transient failure withholds
        a legitimate reset or first Google link, which the person recovers
        from by retrying; the alternative is not recoverable.

        A 500 for an address that has an account beside a 200 for one that
        does not IS an oracle, which is the other reason the failure has to
        become an answer rather than an exception. ``restore_session`` runs
        first in the handler, before the log line, because on an aborted
        transaction even reading ``user.id`` is a query.

        Status comes from :meth:`status_of` and nowhere else (seam S3).
        """
        # Pre-bound and read *inside* the ``try``, the same way
        # :meth:`mark_accepted_if_pending` does it: on an already-aborted
        # transaction ``user.id`` is itself a query, so a snapshot taken above
        # the net would be the one line able to throw past it. Inside, a
        # failure to read it costs the log line an id and nothing else — and
        # the handler must have one, because this is the branch that silently
        # withholds a legitimate reset or first Google link, and "something
        # was refused somewhere" is not something an operator can act on.
        user_id: uuid.UUID | None = None
        try:
            user_id = user.id
            if not InvitationService.is_unclaimed(user):
                return False
            invitation = InvitationService.get_for_user(session, user_id)
            if invitation is None:
                return False
            return (
                InvitationService.status_of(invitation)
                != INVITATION_STATUS_PENDING
            )
        except Exception:  # noqa: BLE001 — see the docstring.
            restore_session(session)
            logger.warning(
                "Could not read the invitation state for user %s while "
                "deciding whether the account may be claimed; refusing.",
                user_id,
                exc_info=True,
            )
            return True

    @staticmethod
    def is_unclaimed(user: User) -> bool:
        """Whether nobody has ever signed in as this account.

        Two columns, because there are exactly two ways an account acquires a
        way in: a password hash, or a linked Google identity. Everything else
        an account can accumulate — sessions, agents, a second factor —
        follows from one of those, so this is the whole question.

        It is a *fact about the row*, deliberately not about the invitation.
        The invitation is the record of an offer and it can be wrong: the two
        places that settle it after a Google login and after a password reset
        (:meth:`mark_accepted_if_pending`) are non-raising by contract and
        leave the row reading ``pending`` when they fail — that is the
        documented, tolerated outcome, not an edge case. So an account can be
        in use and still carry a ``pending`` invitation, and only the columns
        can say so.
        """
        return user.hashed_password is None and user.google_id is None

    @staticmethod
    def is_interrupted_invite(session: Session, user: User) -> bool:
        """Whether ``user`` is the wreckage of a half-finished :meth:`invite`.

        :meth:`invite` commits the account row first and writes the invitation
        several steps later, and the steps in between can raise —
        ``AccessPolicyService.resolve`` inserts and commits the ``ServerConfig``
        singleton on first boot, and ``create_invitation`` commits too. What is
        left behind is an account with no password and no invitation row: too
        much for a re-invite (the duplicate check refuses it) and too little
        for :meth:`resend` (there is no row to rotate), so the admin's only
        exit was deleting the user.

        The shape is narrow on purpose: :meth:`is_unclaimed` excludes every
        account anyone has ever signed in as, and the missing invitation row
        excludes a *pending* invited account — which is still refused as a
        duplicate, with the wizard's "resend instead?" hint, because that is
        the correct answer for it.

        WHAT ELSE FITS THIS SHAPE
        -------------------------
        One thing, and it is not an interrupted invite:
        ``UserService.create_external_user(passwordless=True)`` — the row a
        server channel creates for an inbound Google Chat sender. That is a
        real person who has already interacted with the platform, they simply
        have no way to sign in yet, and inviting them is a reasonable thing
        for an admin to do: it is what turns a sender into a user. So the
        adoption is allowed rather than refused, and it is *disclosed* instead
        — :meth:`invite` logs it and stamps ``adopted_existing_account`` into
        the audit event, so the admin's feed says the address already had an
        account rather than implying one was created.

        ``User`` carries no persisted origin column (``AccountOrigin`` is an
        argument, not a stored field), so telling the two apart would need a
        schema change. If that column ever exists, this predicate is where it
        belongs.
        """
        return (
            InvitationService.is_unclaimed(user)
            and InvitationService.get_for_user(session, user.id) is None
        )

    @staticmethod
    def status_map(
        session: Session, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Invitation status for a page of accounts, in **one** query.

        The batched counterpart to ``status_of(get_for_user(...))``, for the
        admin users list. It lives here rather than in the route because the
        pair it is made of — the fetch and the derivation — is exactly what
        seam S3 says must not be re-assembled by callers.

        Accounts with no invitation are simply absent from the result, which
        is the ``None`` the projection wants: never invited is not a status.

        The batching is the point and is load-bearing: one ``IN`` over the
        page (verified at one query for a 25-row page), never a lookup per
        row. A caller that loops this per user has reintroduced the N+1 it
        exists to prevent.
        """
        ids = list(user_ids)
        if not ids:
            return {}
        return {
            invitation.user_id: InvitationService.status_of(invitation)
            for invitation in session.exec(
                select(UserInvitation).where(
                    col(UserInvitation.user_id).in_(ids)
                )
            ).all()
        }

    @staticmethod
    def to_public(
        session: Session, invitation: UserInvitation
    ) -> UserInvitationPublic:
        """Project one invitation for an administrator.

        ``invited_by_email`` costs one extra lookup because the model declares
        no ``Relationship`` (see its docstring — the absence is what makes the
        database's ``ON DELETE CASCADE`` the deletion mechanism). Acceptable
        for a single row; a list endpoint must batch it rather than calling
        this per row.
        """
        invited_by_email: str | None = None
        if invitation.invited_by_id is not None:
            inviter = session.get(User, invitation.invited_by_id)
            invited_by_email = inviter.email if inviter else None
        return UserInvitationPublic(
            user_id=invitation.user_id,
            status=InvitationService.status_of(invitation),
            auth_hint=invitation.auth_hint,
            include_desktop=invitation.include_desktop,
            expires_at=invitation.expires_at,
            accepted_at=invitation.accepted_at,
            revoked_at=invitation.revoked_at,
            last_sent_at=invitation.last_sent_at,
            send_count=invitation.send_count,
            invited_by_email=invited_by_email,
        )

    # ── Creation ───────────────────────────────────────────────────────

    @staticmethod
    def create_invitation(
        session: Session,
        *,
        user: User,
        invited_by: User | None,
        include_desktop: bool,
        auth_hint: str,
        expires_in_days: int | None = None,
    ) -> tuple[UserInvitation, str]:
        """Create or re-arm this account's invitation and mint its token.

        Returns ``(row, token)`` from one function, and the token is derived
        from the **committed** row's ``jti``. Splitting these — minting in the
        caller, or minting before the commit — is rule 1 in the module
        docstring and the failure it produces is silent.

        Idempotent in shape rather than in effect: an existing row is re-armed
        in place (new ``jti``, new expiry, ``revoked_at`` cleared) because the
        unique index says one invitation per account and because rotating is
        precisely what makes the previous link stop working.
        """
        now = datetime.now(UTC)
        days = (
            expires_in_days
            if expires_in_days is not None
            else settings.INVITATION_EXPIRE_DAYS
        )
        # Snapshotted while the row is certainly loaded: the commit below
        # expires every instance, and the address is needed after it.
        email = user.email
        user_id = user.id

        invitation = InvitationService.get_for_user(session, user_id)
        if invitation is None:
            invitation = UserInvitation(user_id=user_id)
        invitation.token_jti = uuid.uuid4()
        invitation.invited_by_id = invited_by.id if invited_by else None
        invitation.include_desktop = include_desktop
        invitation.auth_hint = auth_hint
        invitation.expires_at = now + timedelta(days=days)
        # Re-arming clears the two states a fresh offer supersedes. It does
        # NOT clear ``accepted_at`` — callers refuse before they get here, and
        # silently un-accepting an account in use would be a far worse bug
        # than the 409 they raise instead.
        invitation.revoked_at = None
        invitation.updated_at = now
        session.add(invitation)
        session.commit()
        session.refresh(invitation)

        token = generate_invitation_token(
            email=email,
            jti=str(invitation.token_jti),
            expires_at=as_utc(invitation.expires_at),
        )
        return invitation, token

    @staticmethod
    def send(
        session: Session,
        *,
        invitation: UserInvitation,
        user: User,
        token: str,
    ) -> bool:
        """Send the invitation email. Returns whether it actually went out.

        Never raises, and the return value is the whole error channel. An
        instance with no SMTP configured is the *default*, the admin handing
        the accept link over in chat is the supported fallback, and a template
        or delivery failure must not lose an invitation whose account and row
        are already committed.

        The access policy is resolved here, on every send, rather than being
        baked into the row at create time — see :meth:`effective_auth_hint`.

        NOTHING IS MAILED TO AN ACCOUNT THAT CANNOT REDEEM IT
        ----------------------------------------------------
        :meth:`_resolve` refuses a deactivated user, so a link mailed to one
        can only answer the deliberately detail-free "no longer valid" — the
        recipient cannot tell that from a forgery and the admin never sees it
        at all. Both doors reach this: ``invite(is_active=False)``, which
        pre-creates an account to activate later, and ``resend`` to an account
        deactivated since it was invited.

        Suppressed here rather than refused at the route, because the
        *invitation* is legitimate and only the mail is premature: the token
        is checked against ``is_active`` at redemption, not at issue, so
        activating the account before the token expires makes the very same
        link work. The admin gets ``email_sent=False`` and a working-once-active
        ``accept_url``; refusing the whole request would have removed the
        pre-create-then-activate flow the ``is_active`` field exists for.

        A suppressed send does not stamp ``last_sent_at``, so it does not arm
        the resend cooldown — an admin can call resend repeatedly and rotate
        ``token_jti`` each time. That is deliberate and it is not new: the
        same is true of the ``emails_enabled`` short-circuit below, which is
        the *default* on a fresh install. Stamping "last sent" for a mail that
        never left would be a lie that later blocks the legitimate resend once
        the account is activated. The path is superuser-only.
        """
        if not settings.emails_enabled:
            return False
        try:
            # Inside the net, not above it: on a row a concurrent delete has
            # taken away, reading ``is_active`` off an expired instance raises
            # rather than answering, and this method promises never to raise.
            if not user.is_active:
                logger.info(
                    "Not mailing the invitation for user %s: the account is "
                    "deactivated and could not redeem the link.",
                    user.id,
                )
                return False
            policy = AccessPolicyService.resolve(session)
            if (
                not InvitationService.password_accepted(policy, user)
                and not policy.google_auth_enabled
            ):
                # The instance offers this person no sign-in method at all, so
                # the link is undeliverable in the only sense that matters.
                # Not merely theoretical: ``google_auth_enabled`` is derived
                # from the environment, while the lockout validation in
                # ``ServerConfigService`` guards only the *transition* into
                # Google-only — an instance that switched password auth off
                # and later dropped its Google client id lands here. The email
                # template has three branches (password / google / either) and
                # none of them is "neither", so without this it would tell the
                # recipient to use a method that does not exist.
                logger.warning(
                    "Not mailing the invitation for user %s: the access "
                    "policy offers them no sign-in method.",
                    user.id,
                )
                return False
            hint = InvitationService.effective_auth_hint(policy, user, invitation)
            invited_by_name: str | None = None
            if invitation.invited_by_id is not None:
                inviter = session.get(User, invitation.invited_by_id)
                if inviter is not None:
                    invited_by_name = inviter.full_name or inviter.email
            email_data = generate_invitation_email(
                email_to=user.email,
                full_name=user.full_name,
                accept_link=InvitationService.build_accept_url(token),
                web_link=settings.FRONTEND_HOST,
                desktop_link=(
                    f"{settings.FRONTEND_HOST}/desktop"
                    if invitation.include_desktop
                    else None
                ),
                auth_hint=hint,
                invited_by_name=invited_by_name,
                expires_in_days=settings.INVITATION_EXPIRE_DAYS,
            )
            send_email(
                email_to=user.email,
                subject=email_data.subject,
                html_content=email_data.html_content,
            )
            invitation.last_sent_at = datetime.now(UTC)
            invitation.send_count = invitation.send_count + 1
            invitation.updated_at = datetime.now(UTC)
            session.add(invitation)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — see the docstring.
            # The commit is inside the ``try``, so a statement-level failure
            # can leave the transaction aborted with ``Session.is_active``
            # still True. Repair before returning, or the caller's next commit
            # dies inside unrelated code.
            restore_session(session)
            logger.error(
                "Failed to send the invitation email: %s", exc, exc_info=True
            )
            return False
        return True

    @staticmethod
    def _resume_interrupted(
        session: Session, *, user: User, data: InviteUserRequest, role: str
    ) -> User:
        """Re-arm an interrupted invite's account from a fresh submission.

        Only the fields the wizard collects are written, and only onto a row
        :meth:`is_interrupted_invite` has already vouched for — an account
        nobody has ever signed into. The admin's latest submission wins over
        the abandoned attempt's, because the alternative is a re-invite that
        silently keeps a role the admin has just changed on screen.

        ``create_account`` stays the authority for everything about a *new*
        row — normalisation, the policy gate, the default-role derivation. The
        two rules restated here are the two this path can actually break: the
        role must be a real one (the request schema checks it too, and this is
        the same belt-and-braces ``create_account`` applies), and
        ``role ⇔ is_superuser`` must hold, which is why the flag is derived
        rather than accepted. Auto-confirmation follows the promotion for the
        same reason ``create_account`` applies it: an unconfirmed superuser has
        its own notifications gated.

        The registration gate is not re-asserted, and there is nothing to
        re-assert: the ``invite`` origin is ungated (``can_register`` returns
        allowed for it unconditionally), and the account this path re-arms was
        already admitted once. If that origin ever becomes gated, the check
        belongs here as well as in ``create_account``.

        WHAT "LATEST SUBMISSION WINS" DOES NOT COVER, AND THE ONE RULE FOR IT
        ---------------------------------------------------------------------
        A field is written **if and only if the submission stated it**, and
        "stated it" is exactly ``is not None``. One test, applied uniformly,
        with no per-field reasoning at this write site: the reasoning lives on
        :class:`InviteUserRequest`, which is what makes omission representable
        in the first place, and its docstring carries the argument.

        Two fields are optional and so reach that test — ``full_name`` (an
        admin who types only an address must not erase the name an adopted row
        already carries) and ``is_active`` (re-inviting an account an
        administrator deliberately deactivated must not reactivate it). They
        were fixed at two different layers by two different mechanisms, which
        is how one of them silently applied to only one field; now neither is
        special here.

        ``role`` is not optional — the wizard always picks one from a list —
        so it is always written, and ``is_superuser`` is derived from it
        rather than accepted, per the invariant above.
        """
        if role not in VALID_USER_ROLES:
            raise ValueError(
                f"Invalid role '{role}'. Must be one of "
                f"{', '.join(VALID_USER_ROLES)}."
            )
        if data.full_name is not None:
            user.full_name = data.full_name
        user.role = role
        user.is_superuser = role == UserRole.ADMIN.value
        if data.is_active is not None:
            user.is_active = data.is_active
        if user.is_superuser and not user.email_confirmed:
            user.email_confirmed = True
            user.email_confirmed_at = datetime.now(UTC)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    @staticmethod
    async def invite(
        session: Session, *, admin: User, data: InviteUserRequest
    ) -> InviteResult:
        """Create an invited account, provision it, and offer it to its owner.

        The ordering is the interesting part. The account row is committed
        first, through the one chokepoint, with ``skip_auto_provision=True``
        so provisioning happens once and with the administrator attributed to
        it. Provisioning cannot fail the invite. Only then is the invitation
        row written and the token minted from it, and only then is the mail
        attempted — so an SMTP outage costs an email, never an account.

        ``is_superuser`` is derived from the role rather than accepted
        separately: ``create_account`` treats ``is_superuser=True`` as pinning
        the role to ``admin`` and auto-confirming, but it will also *accept*
        ``role="admin", is_superuser=False`` and produce a row that breaks the
        ``role ⇔ is_superuser`` invariant. Deriving here is what stops the
        wizard being able to ask for that.

        The stored ``auth_hint`` is not validated against the current policy.
        It is presentational, it is re-resolved at every send
        (:meth:`effective_auth_hint`), and refusing an invite over it would be
        a check on a value this request does not control the future of.

        Raises:
            ValueError: invalid address, duplicate address, invalid role.
            RegistrationNotAllowedError: not reachable in practice — the
                ``invite`` origin is ungated — but propagated rather than
                swallowed so a future gating change is loud.
        """
        role = data.role
        adopted_existing_account = False
        existing = UserService.get_user_by_email(
            session=session, email=data.email.strip().lower()
        )
        if existing is not None and InvitationService.is_interrupted_invite(
            session, existing
        ):
            # The retry path for finding 3: an earlier invite committed this
            # account and then failed before writing its invitation. Re-using
            # the row is what makes re-submitting the wizard the repair, and
            # everything after this point — provisioning, the invitation row,
            # the mail, the audit — runs exactly as it does for a fresh
            # account. ``add_members`` is idempotent, so the credentials the
            # interrupted attempt did grant are not granted twice.
            logger.info(
                "Invite for %s re-used the existing account %s: it had no "
                "sign-in method and no invitation row.",
                data.email,
                existing.id,
            )
            adopted_existing_account = True
            user = InvitationService._resume_interrupted(
                session, user=existing, data=data, role=role
            )
        else:
            user = UserService.create_account(
                session,
                email=data.email,
                origin=AccountOrigin.INVITE,
                password=None,
                full_name=data.full_name,
                role=role,
                is_superuser=(role == UserRole.ADMIN.value),
                # ``None`` is "the admin did not state one" (see
                # :class:`InviteUserRequest`). For a *new* row that means the
                # ordinary default, and the default belongs to
                # ``create_account`` — this restates it rather than omitting
                # the argument only so the call stays typed and the reader
                # does not have to go and look up which way it falls.
                is_active=data.is_active if data.is_active is not None else True,
                skip_auto_provision=True,
            )

        report = AccountProvisioningService.provision_explicit(
            session,
            user,
            AccountOrigin.INVITE,
            managed_credential_ids=data.managed_credential_ids,
            actor=admin,
        )
        provisioning = InviteProvisioningSummary(
            added_count=len(report.added),
            skipped=[
                InviteProvisioningSkip(
                    managed_credential_id=skip.managed_credential_id,
                    reason=skip.reason,
                )
                for skip in report.skipped
            ],
            provisioning_failed=report.failed,
        )

        include_desktop = data.include_desktop
        if include_desktop is None:
            include_desktop = AccessPolicyService.resolve(
                session
            ).invite_include_desktop_default

        invitation, token = InvitationService.create_invitation(
            session,
            user=user,
            invited_by=admin,
            include_desktop=include_desktop,
            auth_hint=data.auth_hint,
        )
        accept_url = InvitationService.build_accept_url(token)

        email_sent = False
        if data.send_email:
            email_sent = InvitationService.send(
                session, invitation=invitation, user=user, token=token
            )

        details = {
            "target_user_id": str(user.id),
            "role": role,
            "auth_hint": invitation.auth_hint,
            "include_desktop": invitation.include_desktop,
            "email_sent": email_sent,
            "provisioned_count": provisioning.added_count,
            "actor": str(admin.id),
            # Disclosure, not bookkeeping: this invite re-used a row that
            # already existed rather than creating one. Also carried on
            # ``InviteUserResponse`` so the wizard can say so at the moment it
            # happens; the audit feed keeps it for afterwards. See
            # :meth:`is_interrupted_invite`.
            "adopted_existing_account": adopted_existing_account,
        }
        # Two feeds, two facts, two types. The invited account's feed records
        # that their account came to exist; the acting admin's records that
        # they did it. Same ``details`` — the shared payload is what makes the
        # pair correlatable — but never the same ``event_type``, because a
        # feed cannot tell "created for you" from "created by you" if both
        # rows say the same word. See EVENT_ADMIN_INVITATION_CREATED.
        await InvitationService._emit(
            session,
            user_id=user.id,
            event_type=EVENT_INVITATION_CREATED,
            severity="low",
            details=details,
        )
        await InvitationService._emit(
            session,
            user_id=admin.id,
            event_type=EVENT_ADMIN_INVITATION_CREATED,
            severity="low",
            details=details,
        )

        return InviteResult(
            user=user,
            invitation=InvitationService.to_public(session, invitation),
            accept_url=accept_url,
            email_sent=email_sent,
            provisioning=provisioning,
            adopted_existing_account=adopted_existing_account,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────

    @staticmethod
    async def resend(
        session: Session, *, admin: User, user_id: uuid.UUID
    ) -> ResendResult:
        """Issue a fresh link for an existing invitation.

        Rotates ``token_jti``, extends the expiry and clears ``revoked_at``,
        which is what makes this the repair action for *every* non-accepted
        state. Expiry is the thing resend fixes, so refusing an expired
        invitation would refuse the only case that needs it; revocation is
        reversible by design, and the UI labels the action "Send new
        invitation" in that state.

        Raises:
            InvitationNotFoundError: this account was never invited.
            InvitationAlreadyAcceptedError: nothing left to offer.
            InvitationCooldownError: too soon since this row's last send.
        """
        invitation = InvitationService.get_for_user(session, user_id)
        if invitation is None:
            raise InvitationNotFoundError(
                "No invitation exists for this account"
            )
        # Through ``status_of``, not off ``accepted_at`` directly: the claim
        # that status is derived in exactly one place (rule 3) is only true if
        # the lifecycle gates ask it too, and reading the column here is how a
        # future change to the ordering — accepted losing to something else —
        # would reach the badge and not this refusal.
        if (
            InvitationService.status_of(invitation)
            == INVITATION_STATUS_ACCEPTED
        ):
            raise InvitationAlreadyAcceptedError(
                "This invitation has already been accepted"
            )

        # Per-row cooldown, checked before anything is mutated so a refused
        # resend leaves the live token alone. Rotating first and then refusing
        # would invalidate a link the recipient may be about to click.
        if invitation.last_sent_at is not None:
            available_at = as_utc(invitation.last_sent_at) + timedelta(
                seconds=settings.INVITATION_RESEND_COOLDOWN_SECONDS
            )
            if datetime.now(UTC) < available_at:
                raise InvitationCooldownError(available_at)

        user = session.get(User, user_id)
        if user is None:
            # The unique FK says this cannot happen without the row having
            # been cascaded away with it; treat it as "no invitation" rather
            # than crashing.
            raise InvitationNotFoundError(
                "No invitation exists for this account"
            )

        # Rotation happens before the send, and unlike the cooldown above it
        # is NOT rolled back when the send fails. The asymmetry is deliberate:
        # the cooldown refuses *without acting*, so leaving the live token
        # alone costs nothing, whereas here the new token is the request's
        # actual product. It is returned to the admin as ``accept_url``, and
        # handing that link over is the supported fallback on the many
        # instances that have no SMTP at all — ``email_sent=False`` is the
        # ordinary case there, not an error. Restoring the previous ``jti``
        # whenever the mail did not go out would make the link this endpoint
        # just handed the admin dead on arrival, which is a worse failure than
        # the one it would fix. The cost, stated plainly: a send that fails
        # after rotation leaves the recipient's older link invalid, and the
        # repair is the admin passing on the returned link or resending.
        invitation, token = InvitationService.create_invitation(
            session,
            user=user,
            invited_by=admin,
            include_desktop=invitation.include_desktop,
            auth_hint=invitation.auth_hint,
        )
        email_sent = InvitationService.send(
            session, invitation=invitation, user=user, token=token
        )

        await InvitationService._emit(
            session,
            user_id=user_id,
            event_type=EVENT_INVITATION_RESENT,
            severity="low",
            details={
                "target_user_id": str(user_id),
                "email_sent": email_sent,
                "send_count": invitation.send_count,
                "actor": str(admin.id),
            },
        )
        return ResendResult(
            invitation=InvitationService.to_public(session, invitation),
            accept_url=InvitationService.build_accept_url(token),
            email_sent=email_sent,
        )

    @staticmethod
    async def revoke(
        session: Session, *, admin: User, user_id: uuid.UUID
    ) -> UserInvitationPublic:
        """Withdraw an outstanding invitation.

        Idempotent: revoking an already-revoked invitation returns the current
        projection rather than 409, and revoking an *expired* one succeeds —
        expiry is not a state this request transitions, and refusing over it
        would refuse a request that changes nothing the caller cares about.
        Only acceptance blocks, because acceptance is the one state where
        there is genuinely nothing left to revoke.

        The row and its ``token_jti`` are kept, not deleted. That is what stops
        a revoked token being replayed if the row is later re-armed by resend:
        the jti it carries is no longer the live one.

        Raises:
            InvitationNotFoundError, InvitationAlreadyAcceptedError.
        """
        invitation = InvitationService.get_for_user(session, user_id)
        if invitation is None:
            raise InvitationNotFoundError(
                "No invitation exists for this account"
            )
        # Rule 3, as in :meth:`resend`.
        if (
            InvitationService.status_of(invitation)
            == INVITATION_STATUS_ACCEPTED
        ):
            raise InvitationAlreadyAcceptedError(
                "This invitation has already been accepted"
            )
        if invitation.revoked_at is None:
            now = datetime.now(UTC)
            invitation.revoked_at = now
            invitation.updated_at = now
            session.add(invitation)
            session.commit()
            session.refresh(invitation)
            await InvitationService._emit(
                session,
                user_id=user_id,
                event_type=EVENT_INVITATION_REVOKED,
                severity="medium",
                details={
                    "target_user_id": str(user_id),
                    "actor": str(admin.id),
                },
            )
        return InvitationService.to_public(session, invitation)

    @staticmethod
    async def issue_link(
        session: Session, *, admin: User, user_id: uuid.UUID
    ) -> tuple[str, datetime]:
        """Read out the live accept link without rotating it.

        Returns ``(accept_url, expires_at)``.

        Deliberately not :meth:`resend`: rotating ``token_jti`` here would
        invalidate the email the person may be about to click, purely because
        an admin wanted to read the link out. So the token is minted from the
        ``jti`` already on the row, which keeps the outstanding link the only
        live one.

        Refused for anything but a pending invitation. The link for a revoked,
        expired or accepted one passes signature and purpose verification and
        is then rejected by :meth:`_resolve` as indistinguishable from a
        forgery, so handing it over would be handing over a dud. Resend is the
        action for those.

        This composition — resolve the row, check the status, mint from the
        stored ``jti``, audit — lived in the route, where nothing stopped the
        next reader from re-implementing the status check or minting a fresh
        ``jti``. It is a service method for the same reason
        :meth:`create_invitation` returns its token: the mint and the row it
        is minted from must not be separable by a caller.

        Raises:
            InvitationNotFoundError: no invitation, or no such account.
            InvitationNotPendingError: there is no live link to read out.
        """
        invitation = InvitationService.get_for_user(session, user_id)
        if invitation is None:
            raise InvitationNotFoundError(
                "No invitation exists for this account"
            )
        current_status = InvitationService.status_of(invitation)
        if current_status != INVITATION_STATUS_PENDING:
            raise InvitationNotPendingError(current_status)
        user = session.get(User, user_id)
        if user is None:
            # The unique FK makes this unreachable without the row having been
            # cascaded away with the account; answered as "no invitation" for
            # the same reason :meth:`resend` does.
            raise InvitationNotFoundError(
                "No invitation exists for this account"
            )

        token = generate_invitation_token(
            email=user.email,
            jti=str(invitation.token_jti),
            expires_at=as_utc(invitation.expires_at),
        )
        expires_at = invitation.expires_at
        # After the token is built, because the audit write commits and would
        # otherwise expire the row the mint reads ``token_jti`` off.
        await InvitationService.record_link_issued(
            session, admin=admin, user_id=user_id
        )
        return InvitationService.build_accept_url(token), expires_at

    @staticmethod
    async def record_link_issued(
        session: Session, *, admin: User, user_id: uuid.UUID
    ) -> None:
        """Audit that an administrator read out an account's accept link.

        Handing over the link signs the recipient in as that account, so it is
        recorded even though the admin already controls the account and the
        act grants them nothing new. Non-raising: an audit write must not be
        what fails a read.
        """
        await InvitationService._emit(
            session,
            user_id=user_id,
            event_type=EVENT_INVITATION_LINK_ISSUED,
            severity="medium",
            details={"target_user_id": str(user_id), "actor": str(admin.id)},
        )

    # ── The public surface ─────────────────────────────────────────────

    @staticmethod
    def _resolve(
        session: Session, token: str
    ) -> tuple[UserInvitation, User] | None:
        """Every check a token must pass, in one place. ``None`` means no.

        Deliberately returns a single ``None`` for eight distinct reasons —
        see NON-ENUMERATION in the module docstring. The order below is the
        order the cost increases in: purpose and signature before any database
        access at all, then one indexed SELECT.
        """
        verified = verify_invitation_token(token)
        if verified is None:
            return None
        token_email, raw_jti = verified
        try:
            jti = uuid.UUID(raw_jti)
        except (ValueError, AttributeError, TypeError):
            return None

        invitation = session.exec(
            select(UserInvitation).where(UserInvitation.token_jti == jti)
        ).first()
        if invitation is None:
            return None
        # Rule 3: the one derivation, compared to the one acceptable value.
        if InvitationService.status_of(invitation) != INVITATION_STATUS_PENDING:
            return None

        user = session.get(User, invitation.user_id)
        if user is None or not user.is_active:
            return None
        # The row says ``pending``; the account says otherwise. A pending
        # invitation is not proof the offer is outstanding, because the two
        # functions that settle it — :meth:`mark_accepted_if_pending`, called
        # from the Google callback and from ``POST /reset-password/`` — are
        # non-raising by contract and leave the row pending when they fail.
        # Without this check, one transient failure there turns a live invite
        # link into a password-overwrite primitive: :meth:`accept_with_password`
        # would set a new password on an account already in use, and the
        # accept route mints a session token with no second-factor branch
        # (there is none to take, on the premise that a redeeming account
        # cannot have enrolled one — a premise only this line makes true).
        if not InvitationService.is_unclaimed(user):
            return None
        # The independent second check (rule 2). The token resolved the row by
        # ``jti``; this confirms it still describes the same person. Compared
        # case-insensitively because ``create_account`` stores addresses
        # lowercased while the unique index is case-sensitive — folding here
        # means a legacy mixed-case row cannot produce an invitation nobody
        # can accept.
        if token_email.strip().lower() != (user.email or "").strip().lower():
            return None
        return invitation, user

    @staticmethod
    def lookup(session: Session, token: str) -> InvitationLookupPublic:
        """What the accept page is allowed to know before anyone signs in.

        An unresolvable token answers ``valid=False`` and nothing else — the
        route serialises with ``response_model_exclude_none=True`` so every
        other field is *absent*, not null. ``password_accepted`` in particular
        must be absent rather than ``false``: a field that is always present
        with a boolean value would separate a real token from a forged one on
        any instance that allows password auth.

        Reads only. No row is written and no email is sent, on either path.
        """
        resolved = InvitationService._resolve(session, token)
        if resolved is None:
            return InvitationLookupPublic(valid=False)
        invitation, user = resolved
        policy = AccessPolicyService.resolve(session)
        return InvitationLookupPublic(
            valid=True,
            email_masked=InvitationService.mask_email(user.email),
            full_name=user.full_name,
            auth_hint=InvitationService.effective_auth_hint(
                policy, user, invitation
            ),
            # Rule 4. The same call ``accept_with_password`` enforces, so the
            # page cannot render a form the endpoint will refuse.
            password_accepted=InvitationService.password_accepted(policy, user),
            google_auth_enabled=policy.google_auth_enabled,
            include_desktop=invitation.include_desktop,
            project_name=settings.PROJECT_NAME,
            expires_at=invitation.expires_at,
        )

    @staticmethod
    async def accept_with_password(
        session: Session, *, token: str, password: str, full_name: str | None
    ) -> User:
        """Redeem an invitation by setting a password. Returns the signed-in user.

        The password write and ``accepted_at`` land in the same transaction,
        which is what makes the token single-use: a second attempt finds a
        non-pending status and is refused exactly like a forgery.

        Clicking a link only the invitee received proves control of the
        address, so acceptance confirms the email as well — the same reasoning
        the Google path already relies on.

        There is **no 403 branch**. A person the policy does not allow to use a
        password gets the same :class:`InvitationInvalidError` as a forged
        token, because a distinct status here would answer "is this token
        real". They never see it: the page they came from asked
        :meth:`lookup`, got ``password_accepted=false`` from this same
        predicate, and did not render a form.

        Raises:
            InvitationInvalidError: for every failure, without exception.
        """
        resolved = InvitationService._resolve(session, token)
        if resolved is None:
            raise InvitationInvalidError()
        invitation, user = resolved

        policy = AccessPolicyService.resolve(session)
        if not InvitationService.password_accepted(policy, user):
            logger.info(
                "Invitation for user %s was accepted with a password that "
                "policy does not allow; answering generically.",
                user.id,
            )
            raise InvitationInvalidError()

        now = datetime.now(UTC)
        user.hashed_password = get_password_hash(password)
        if full_name:
            user.full_name = full_name
        invitation.accepted_at = now
        invitation.updated_at = now
        session.add(user)
        session.add(invitation)
        session.commit()
        session.refresh(user)
        # Snapshotted while the session is known good: everything below is
        # netted, and a handler that reads ``user.id`` off an aborted
        # transaction throws from inside itself.
        accepted_user_id = user.id

        # Netted for the same reason ``_emit`` below is, and it is the same
        # hazard: this runs *after* the commit that set the password and
        # stamped the invitation accepted, and it commits on its own, so it
        # can raise. Unguarded, a failure here 500s someone whose password is
        # already set and whose invitation is already terminal — and their
        # retry gets the generic "no longer valid", with no way to learn that
        # they can simply sign in. An unconfirmed address is a far smaller
        # problem than that, and the admin can see it.
        try:
            EmailConfirmationService.mark_confirmed(session=session, user=user)
        except Exception:  # noqa: BLE001 — see the comment above.
            # ``restore_session`` first, before the log line touches ``user``:
            # on an aborted transaction reading an expired attribute is itself
            # a query. ``user_id`` is snapshotted above the call for the same
            # reason.
            restore_session(session)
            logger.warning(
                "Could not mark the address of user %s confirmed after "
                "invitation acceptance; the password was set and the "
                "invitation is accepted.",
                accepted_user_id,
                exc_info=True,
            )
        await InvitationService._emit(
            session,
            user_id=accepted_user_id,
            event_type=EVENT_INVITATION_ACCEPTED,
            severity="low",
            details={
                "target_user_id": str(accepted_user_id),
                "method": "password",
            },
        )
        return user

    @staticmethod
    async def mark_accepted_if_pending(
        session: Session, user: User, *, method: str
    ) -> bool:
        """Settle an outstanding invitation for someone who just authenticated.

        Called from the Google callback and from ``POST /reset-password/``,
        where the person has already proved who they are — by an identity
        Google vouched for, or by possession of a server-signed reset token.

        **Never raises, by contract and by construction**, and the contract is
        the point: invitation bookkeeping must never cost someone an
        authentication they already completed. If this fails, the correct
        outcome is a signed-in person and a stale ``pending`` row the admin can
        see and resend — not a failed login. It is built the same way
        ``AccountProvisioningService.on_account_created`` is, and for the same
        reason, rather than being merely wrapped in a ``try`` at the call site:

        * identifiers are snapshotted while the session is known good, because
          on an aborted transaction even ``user.id`` is a query and a handler
          that reads one to build its log line throws from inside itself;
        * ``restore_session`` runs **first** in the handler, before any
          logging and before any audit write;
        * the session is left usable, because both callers commit after this —
          the reset route has already committed the new password and the
          Google route mints a token.

        Only a ``pending`` invitation is settled. An expired or revoked one is
        left as it stands: the admin's revocation is not undone by the person
        arriving through another door, and the row stays honest about how the
        offer ended.

        Returns whether a row was actually marked, for tests and callers that
        want to log it. Callers must not branch on it for anything the user
        can observe.
        """
        user_id: uuid.UUID | None = None
        try:
            user_id = user.id
            invitation = InvitationService.get_for_user(session, user_id)
            if invitation is None:
                return False
            if (
                InvitationService.status_of(invitation)
                != INVITATION_STATUS_PENDING
            ):
                return False
            now = datetime.now(UTC)
            invitation.accepted_at = now
            invitation.updated_at = now
            session.add(invitation)
            session.commit()
        except Exception:  # noqa: BLE001 — see the docstring.
            restore_session(session)
            logger.warning(
                "Could not mark the invitation for user %s accepted "
                "(method=%s); the authentication stands and the invitation "
                "remains pending.",
                user_id,
                method,
                exc_info=True,
            )
            return False

        await InvitationService._emit(
            session,
            user_id=user_id,
            event_type=EVENT_INVITATION_ACCEPTED,
            severity="low",
            details={"target_user_id": str(user_id), "method": method},
        )
        return True

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    async def _emit(
        session: Session,
        *,
        user_id: uuid.UUID,
        event_type: str,
        severity: str,
        details: dict[str, object],
    ) -> None:
        """Write one audit event. Best-effort — never raises.

        Every caller here has already committed the thing the event describes:
        an account exists, an invitation was rotated, a password was set. An
        audit row that cannot be written must not be what takes those away, so
        the failure is logged and the session repaired — in that order, since
        ``restore_session`` has to run before anything else touches it.
        """
        try:
            await SecurityEventService.create_event(
                session,
                user_id,
                SecurityEventCreate(
                    event_type=event_type,
                    severity=severity,
                    details=details,
                ),
            )
        except Exception:  # pragma: no cover - defensive
            restore_session(session)
            logger.exception(
                "Failed to record invitation event %s for user %s.",
                event_type,
                user_id,
            )


__all__ = [
    "EVENT_ADMIN_INVITATION_CREATED",
    "EVENT_INVITATION_ACCEPTED",
    "EVENT_INVITATION_CREATED",
    "EVENT_INVITATION_LINK_ISSUED",
    "EVENT_INVITATION_RESENT",
    "EVENT_INVITATION_REVOKED",
    "InvitationAlreadyAcceptedError",
    "InvitationCooldownError",
    "InvitationError",
    "InvitationInvalidError",
    "InvitationNotFoundError",
    "InvitationNotPendingError",
    "InvitationService",
    "InviteResult",
    "ResendResult",
]
