"""The invitation an administrator issues, and everything it is projected as.

WHAT THIS IS
------------
Zero-touch onboarding phase 3. An admin invites a person by address; the
account row is created immediately through the usual chokepoint
(``UserService.create_account(origin=AccountOrigin.INVITE)``) with no password,
and **one** ``user_invitation`` row records the outstanding offer. The person
accepts by setting a password or by signing in with Google, at which point the
row is stamped ``accepted_at`` and the account behaves like any other.

One row per account, reused for the lifetime of the account: resending rotates
``token_jti`` in place rather than inserting a second row. That is what makes
revocation meaningful — there is exactly one live ``jti`` per invitation, so an
old emailed link stops working the moment a new one is issued.

TWO THINGS IN HERE ARE LOAD-BEARING AND EASY TO UNDO
----------------------------------------------------
**No ``Relationship`` back to ``User``.** Deleting a user must cascade the
invitation, and ``users.delete_user`` is a bare ``session.delete(user)`` that
relies entirely on the database's ``ON DELETE CASCADE``. An unconfigured
SQLModel relationship would make SQLAlchemy try to NULL a ``NOT NULL``
``user_id`` first and raise ``IntegrityError`` instead of letting Postgres do
the cascade. The FK ``ondelete=`` declarations below are the whole mechanism.

**The status strings live here, once.** ``status_of`` on ``InvitationService``
is the only code that derives one; every consumer — the admin projection, the
public lookup, the accept check, the users-list badge — compares against these
constants. A fifth value invented somewhere else would fall through the
frontend's switch to a default badge with no type error, because the field
travels as ``str | None``.
"""
import uuid
from datetime import UTC, datetime

from pydantic import EmailStr, field_validator
from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel

from app.models.users.user import VALID_USER_ROLES, UserPublic

# ── Status vocabulary ──────────────────────────────────────────────────
#
# Derived, never stored: an invitation's status is a function of its three
# timestamps and the clock. Ordering matters and is fixed in
# ``InvitationService.status_of`` — accepted wins over revoked wins over
# expired — so that a revoked-then-expired row does not read differently
# depending on which check ran first.
INVITATION_STATUS_ACCEPTED = "accepted"
INVITATION_STATUS_REVOKED = "revoked"
INVITATION_STATUS_EXPIRED = "expired"
INVITATION_STATUS_PENDING = "pending"

#: Every value ``status_of`` may return, and therefore every value
#: ``UserPublic.invitation_status`` may carry. One tuple, so a new status is a
#: change to this line rather than a string appearing in a service and
#: silently reaching a frontend switch statement.
VALID_INVITATION_STATUSES = (
    INVITATION_STATUS_ACCEPTED,
    INVITATION_STATUS_REVOKED,
    INVITATION_STATUS_EXPIRED,
    INVITATION_STATUS_PENDING,
)

# ── Sign-in hint ───────────────────────────────────────────────────────
#
# PRESENTATIONAL ONLY. The hint decides which method the invitation email and
# the accept page *lead with*; it never decides which methods are available.
# Whether the invitee may use a password is answered once, server-side, by
# ``AccessPolicyService.is_password_auth_allowed`` and surfaced as
# ``InvitationLookupPublic.password_accepted``. A hint that gated anything
# would be a second implementation of that policy question, and the two would
# drift.
INVITATION_AUTH_HINT_ANY = "any"
INVITATION_AUTH_HINT_PASSWORD = "password"
INVITATION_AUTH_HINT_GOOGLE = "google"

VALID_INVITATION_AUTH_HINTS = (
    INVITATION_AUTH_HINT_ANY,
    INVITATION_AUTH_HINT_PASSWORD,
    INVITATION_AUTH_HINT_GOOGLE,
)


class UserInvitation(SQLModel, table=True):
    """One outstanding (or settled) invitation, one per account."""

    __tablename__ = "user_invitation"
    __table_args__ = (
        # Declared as explicit unique indexes rather than through
        # ``Field(unique=True)`` so the names in the migration and the names
        # autogenerate would propose are the same string. Both are also the
        # only read paths: every lookup is by ``user_id`` (admin projections)
        # or by ``token_jti`` (token verification).
        #
        # There is deliberately NO index on ``expires_at``. Nothing queries by
        # it — expiry is evaluated on a row already fetched by one of the two
        # keys above. The sweeper that would want it does not exist yet; see
        # the migration's comment for what to do when it lands.
        Index("uq_user_invitation_user_id", "user_id", unique=True),
        Index("uq_user_invitation_token_jti", "token_jti", unique=True),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # CASCADE is the whole deletion story — see the module docstring.
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # The live token's ``jti``. Rotated on every resend, which is what makes a
    # previously emailed link stop working: verification loads the invitation
    # *by this column*, so a token carrying a rotated-away jti finds nothing
    # and is refused exactly like a forgery.
    token_jti: uuid.UUID = Field(default_factory=uuid.uuid4, nullable=False)
    # SET NULL, not CASCADE: deleting the admin who issued an invitation must
    # not delete the invitations they issued, or removing a departing
    # administrator would silently invalidate every outstanding invite.
    invited_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    auth_hint: str = Field(default=INVITATION_AUTH_HINT_ANY, max_length=16)
    include_desktop: bool = Field(default=True)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    accepted_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    last_sent_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    send_count: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
    )


# ── Admin-facing projections ───────────────────────────────────────────


class UserInvitationPublic(SQLModel):
    """The invitation as an administrator sees it.

    Superuser-only, so ``invited_by_email`` is not a disclosure. It is not a
    relationship traversal — there is no ``Relationship`` on the table — so the
    service resolves it, and a list endpoint must batch that lookup rather than
    doing one per row.
    """

    user_id: uuid.UUID
    status: str
    auth_hint: str
    include_desktop: bool
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    last_sent_at: datetime | None = None
    send_count: int = 0
    invited_by_email: str | None = None


class InviteProvisioningSkip(SQLModel):
    """One managed AI credential the invited account did *not* receive.

    ``reason`` is the machine-readable string ``AccountProvisioningService``
    already produces. The full vocabulary, because the wizard renders copy per
    reason and a value it has never heard of falls through to a blank line:

    * ``user_not_found`` — from the shared reconcile path;
    * ``user_inactive`` — from ``_provision``'s inactive short-circuit,
      which is in practice the only producer this wizard ever sees: the
      short-circuit returns before ``add_members`` is called, so the
      reconcile path's own ``user_inactive`` cannot be reached from here.
      One entry per **requested** credential, so that a deactivated
      account does not report the empty summary an admin who ticked
      nothing gets;
    * ``managed_credential_not_found`` — an id the admin ticked no longer
      exists, or is not a managed credential. Reachable **only** from the
      explicit invite path (``provision_explicit``), which is exactly this
      model's path, so it is the one reason the automatic path never emits and
      the one most likely to be missing from the frontend's map;
    * ``provision_failed`` — the per-parent guard caught something;
    * ``add_members_failed`` — the grant itself failed for this credential.
    """

    managed_credential_id: uuid.UUID
    reason: str


class InviteProvisioningSummary(SQLModel):
    """What the invited account was granted, for the wizard's success screen.

    Deliberately not ``ManagedAICredentialReconcileResult``: that shape carries
    ``removed``/``blocked``/``updated``, which an add-only grant can never
    populate, and a ``record`` projection whose construction costs a per-member
    user lookup and a key decrypt. None of it is read here.
    """

    added_count: int = 0
    skipped: list[InviteProvisioningSkip] = Field(default_factory=list)
    # True when provisioning fell over as a whole rather than reporting
    # per-credential results. Without it the wizard cannot tell that state
    # apart from an admin who deliberately unticked everything — both arrive
    # as ``added_count=0, skipped=[]``. The invitation itself is unaffected
    # either way (provisioning can never fail an invite), so this is a
    # disclosure, not an error.
    provisioning_failed: bool = False


# ── Request / response models ──────────────────────────────────────────


class InviteUserRequest(SQLModel):
    """The invite wizard's submission.

    ONE RULE: ``None`` MEANS "THE ADMIN DID NOT SAY"
    ------------------------------------------------
    Every field here that describes *persistent state of the account* is
    optional and nullable, and ``None`` on it means the submission was silent
    about that state — never "set it to the default". The distinction is only
    visible on the adoption path (``InvitationService._resume_interrupted``,
    which re-invites an account that already exists), and there it is the
    whole ballgame: a field that cannot represent its own absence overwrites
    whatever the row already held, every time, and does it silently.

    That is not hypothetical here. ``is_active`` was ``bool = True``, so
    re-inviting an account an administrator had *deliberately deactivated*
    reactivated it — from a wizard that does not even show an active toggle,
    because the field's only real purpose is the pre-create-then-activate
    flow. ``full_name`` had the same shape one step further along: it is
    nullable, but a blank string is not ``None``, so an admin who typed only
    an address erased the name an adopted row already carried. Both are the
    same bug, and this class is where it is fixed once: omission is
    *representable*, and ``_resume_interrupted`` writes a field if and only if
    it is not ``None``. There is no per-field special case at the write site,
    because a rule that lives at one write site is a rule the next write site
    does not have.

    The normaliser below is what makes ``full_name`` obey it: a blank or
    whitespace-only name is an omission spelled differently, so it becomes
    ``None`` at the edge rather than being tested for at the point of use.
    Clearing a name is a real thing to want and the user edit form is where
    it belongs; the invite wizard does not offer it.

    ``managed_credential_ids`` is the same rule with a third state, and it
    already had it: ``None`` means *grant whatever this role would have been
    auto-provisioned anyway*, which is the same predicate every other arrival
    path runs, while ``[]`` means the admin deliberately unticked everything.
    """

    email: EmailStr = Field(max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    role: str = Field(max_length=32)
    # ``None`` → fall back to ``ServerConfig.invite_include_desktop_default``,
    # resolved server-side so the wizard cannot go stale against the policy.
    include_desktop: bool | None = None
    auth_hint: str = Field(
        default=INVITATION_AUTH_HINT_ANY, max_length=16
    )
    managed_credential_ids: list[uuid.UUID] | None = None
    # NOT account state: an instruction for *this request*, which either sends
    # a mail or does not and cannot overwrite anything. A default is correct
    # for it, and "did not say" genuinely does mean "yes, send it".
    send_email: bool = True
    # ``None`` → the admin did not state an activation state. A brand-new
    # account still defaults to active (``UserService.create_account`` owns
    # that default); an adopted one keeps whatever it already had, including a
    # deliberate deactivation.
    is_active: bool | None = None

    @field_validator("full_name")
    @classmethod
    def _blank_name_is_omission(cls, value: str | None) -> str | None:
        """A blank name is an omission, spelled differently. Normalise it.

        Done here rather than at the write site so that "did the admin state a
        name" has one answer — ``full_name is not None`` — everywhere, and so
        that the answer cannot be different in the next place that asks.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        # ``create_account`` validates this too and is the authority; checking
        # at the edge as well is what turns a garbage role into a 422 naming
        # the field instead of a 400 naming nothing.
        if value not in VALID_USER_ROLES:
            raise ValueError(
                f"Invalid role '{value}'. Must be one of "
                f"{', '.join(VALID_USER_ROLES)}."
            )
        return value

    @field_validator("auth_hint")
    @classmethod
    def _validate_auth_hint(cls, value: str) -> str:
        if value not in VALID_INVITATION_AUTH_HINTS:
            raise ValueError(
                f"Invalid auth hint '{value}'. Must be one of "
                f"{', '.join(VALID_INVITATION_AUTH_HINTS)}."
            )
        return value


class InviteUserResponse(SQLModel):
    """What the wizard gets back.

    ``accept_url`` is returned whether or not the email went out: an instance
    with no SMTP configured is the default, and the admin handing the link over
    in chat is the supported fallback. The admin already controls the account,
    so the link grants nothing they did not already have — it is audited all
    the same.

    ``adopted_existing_account`` is disclosure, not bookkeeping. An invite
    that re-used a row which already existed — an interrupted earlier attempt,
    or the passwordless account a server channel created for an inbound sender
    — is otherwise indistinguishable from a fresh creation, so the wizard
    would tell the admin "X now has an account waiting to be claimed" about an
    account that has been there for weeks. It was audit-only while the
    generated client lagged the schema; the field is the better home and this
    is it.
    """

    user: UserPublic
    invitation: UserInvitationPublic
    accept_url: str
    email_sent: bool
    provisioning: InviteProvisioningSummary
    adopted_existing_account: bool = False


class ResendInvitationResponse(SQLModel):
    """What the admin gets back from a resend.

    Carries ``accept_url`` for the same reason :class:`InviteUserResponse`
    does: the link is the supported fallback on an instance with no SMTP, and
    a resend that only reported ``email_sent=False`` would leave the admin
    holding a rotated — therefore newly useless — old link.
    """

    invitation: UserInvitationPublic
    accept_url: str
    email_sent: bool


class InvitationLinkPublic(SQLModel):
    """The live accept link, read out without rotating anything.

    Distinct from resend by design: resend rotates ``token_jti`` and sends
    mail, this one reads out the link that is already outstanding, so an admin
    reading it to a person over the phone does not invalidate the email that
    person may be about to click. It is audited all the same — handing the
    link over signs the recipient in as that account.
    """

    accept_url: str
    expires_at: datetime


class InvitationLookupRequest(SQLModel):
    """Body of ``POST /invitations/lookup``.

    The token travels in the body, not the path: a JWT in a URL is written to
    every proxy access log, kept in browser history and leaked through
    ``Referer``. Same reasoning, same shape as ``NewPassword.token``.
    """

    token: str


class InvitationLookupPublic(SQLModel):
    """What an anonymous caller learns from a token. Read the invariant.

    **Every field except ``valid`` is optional and must be omitted — not sent
    as null — when the token does not resolve.** The route serialises with
    ``response_model_exclude_none=True`` and the contract is that an invalid
    token answers ``200 {"valid": false}`` and nothing else, byte for byte, for
    a forged token, a cross-purpose token, a rotated-away jti, an expired,
    revoked or already-accepted invitation, a deleted or deactivated user, and
    an address the admin has since changed.

    ``password_accepted`` in particular must be **absent** rather than
    ``false``. A field that is always present is a channel: on an instance
    that allows password auth, ``true`` versus absent would separate a real
    token from a forged one, which is the enumeration this endpoint exists to
    avoid.

    ``password_accepted`` is the *single* server-side answer to "may this
    person set a password", computed by
    ``AccessPolicyService.is_password_auth_allowed(policy, user)`` — the same
    call ``accept`` enforces. The client renders the password form on this
    boolean and on nothing else; it must not recombine
    ``password_auth_enabled`` with ``auth_hint`` or with anything else, and
    ``password_auth_enabled`` is deliberately not on this projection so it
    cannot try.
    """

    valid: bool
    email_masked: str | None = None
    full_name: str | None = None
    # Presentational: which method to lead with. Never a gate.
    auth_hint: str | None = None
    password_accepted: bool | None = None
    google_auth_enabled: bool | None = None
    include_desktop: bool | None = None
    project_name: str | None = None
    expires_at: datetime | None = None


class AcceptInvitationRequest(SQLModel):
    """Body of ``POST /invitations/accept``.

    The 8–128 bound is the same one ``UserRegister`` uses, and it is enforced
    here so a too-short password is a 422 about the password rather than being
    folded into the deliberately detail-free 400 that every *token* failure
    returns.
    """

    token: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


__all__ = [
    "INVITATION_AUTH_HINT_ANY",
    "INVITATION_AUTH_HINT_GOOGLE",
    "INVITATION_AUTH_HINT_PASSWORD",
    "INVITATION_STATUS_ACCEPTED",
    "INVITATION_STATUS_EXPIRED",
    "INVITATION_STATUS_PENDING",
    "INVITATION_STATUS_REVOKED",
    "VALID_INVITATION_AUTH_HINTS",
    "VALID_INVITATION_STATUSES",
    "AcceptInvitationRequest",
    "InvitationLinkPublic",
    "InvitationLookupPublic",
    "InvitationLookupRequest",
    "InviteProvisioningSkip",
    "InviteProvisioningSummary",
    "InviteUserRequest",
    "InviteUserResponse",
    "ResendInvitationResponse",
    "UserInvitation",
    "UserInvitationPublic",
]
