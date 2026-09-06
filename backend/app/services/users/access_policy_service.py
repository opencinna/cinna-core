"""AccessPolicyService — the one place the instance's front-door policy is read.

WHAT THIS ANSWERS
-----------------
"May this person get an account here, and how may they sign in?" Six admin
switches on ``ServerConfig`` plus two facts derived from deployment settings
(is Google OAuth configured, is the desktop client enabled) decide what the
login page renders, whether a signup succeeds, whether a Google first-login
creates an account, what role that account gets, and whether a user may edit
their own email address. They are read here, together, once.

**Nothing else may re-derive these rules.** Not the routes, not
``UserService``, not ``AuthService``, and above all not the frontend. Two
implementations of "is registration open" do not stay equal; they drift into a
signup page that shows a form the API will refuse, which is undiagnosable from
either side. The shape is deliberately the same as ``ChannelPolicyService``:
resolve once, hand back plain frozen data, never an ORM row.

WHY THE RETURN VALUE IS A FROZEN DATACLASS
------------------------------------------
``AccessPolicy`` is read on the login path and on every ``UserPublic``
projection, sometimes far from the session that produced it. An ORM row
crossing that boundary turns the reader's next attribute access into a lazy
reload against a possibly-closed session. So: bools and strings only.

THE ENV SETTINGS THIS REPLACES
------------------------------
``AUTH_WHITELIST_USER_DOMAINS`` and ``DEFAULT_USER_ROLE`` are **seed-only**
from here on (``ServerConfigService.get_or_create`` converts them on first
boot). Nothing reads them for a live decision, because a setting that is
sometimes authoritative and sometimes shadowed by the database is the worst of
both: an admin edits the page, nothing changes, and no error is raised.
``warn_if_env_overrides_present`` names the admin page at startup when either
is still set.

PATTERNS GATE REGISTRATION, NOT LOGIN
-------------------------------------
``allowed_email_patterns`` is checked when an account is *created* and never
again. Editing the list does not lock existing users out — the same rule the
retired domain whitelist had, and the one an admin editing a domain list will
assume. Likewise ``registration_mode``: flipping to invite-only stops new
accounts, it does not evict anyone.

EMPTY MEANS "EVERYONE" HERE
---------------------------
``match_email_pattern`` fails closed — an empty pattern string matches nobody.
That is right for a channel's sender allowlist and wrong for this list, where
empty is the backward-compatible "anyone may sign up" the platform shipped
with. The inversion lives in ``is_email_allowed`` and nowhere else, so a caller
can never accidentally get the channel semantics.

And "empty" itself is decided in one place, ``normalize_email_patterns``: a
string of nothing but separators is non-empty to a reader that tests
truthiness and empty to one that splits, and those two readers disagreeing is
a silently closed instance the admin page reports as open. It runs on write
(so the column is canonical) and again in ``resolve`` (so a row written before
it existed reads the same). The ``.strip()`` truthiness tests still standing
in ``is_email_allowed`` and ``can_change_email`` are therefore dead for any
policy that came from ``resolve`` — they are kept as a guard for a directly
constructed ``AccessPolicy``, not as a second opinion about what empty means.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from app.core.config import settings
from app.models.server_config.server_config import (
    REGISTRATION_MODE_OPEN,
    VALID_REGISTRATION_MODES,
    AccessPolicyPublic,
    ServerConfig,
    ServerConfigUpdate,
)
from app.models.users.user import AccountOrigin, User, UserRole
from app.services.common.email_patterns import match_email_pattern

logger = logging.getLogger(__name__)


# ── Reason codes ───────────────────────────────────────────────────────
#
# Stable machine-readable strings, not prose. They travel to the frontend as
# the HTTP ``detail`` so the admin card and the signup page can render their
# own wording (and translate it) instead of pattern-matching English.
#
# **Detail contract.** ``detail`` is one of these codes, optionally followed
# by ``":"`` and a single offending value (only ``invalid_email_pattern`` does
# this, so the admin is told *which* entry to fix). Readers compare
# ``detail.split(":", 1)[0]`` against these constants; the remainder is opaque
# display text.

# Registration refusals
REASON_REGISTRATION_CLOSED = "registration_closed"
REASON_EMAIL_NOT_ALLOWED = "email_not_allowed"
REASON_PASSWORD_AUTH_DISABLED = "password_auth_disabled"
REASON_GOOGLE_AUTO_REGISTER_DISABLED = "google_auto_register_disabled"

# Admin-update (lockout) refusals
REASON_GOOGLE_OAUTH_NOT_CONFIGURED = "google_oauth_not_configured"
REASON_NO_ADMIN_GOOGLE_ACCOUNT = "no_admin_google_account"
REASON_NO_ADMIN_PASSWORD = "no_admin_password"
REASON_INVALID_REGISTRATION_MODE = "invalid_registration_mode"
REASON_INVALID_DEFAULT_USER_ROLE = "invalid_default_user_role"
REASON_INVALID_EMAIL_PATTERN = "invalid_email_pattern"


# ── Origins ────────────────────────────────────────────────────────────
#
# Where an account-creation attempt came from. The enum itself lives on
# ``app.models.users.user`` next to ``UserRole`` — see its comment for why.
# What lives here is the *policy* reading of it: which origins are gated.
#
# Origins that carry their own authority and are never gated here:
#
# - ``admin``    — a superuser deliberately creating an account. Admin intent
#                  outranks the self-service policy; that is the whole point of
#                  an admin-created user.
# - ``invite``   — the invitation itself was the admission decision.
# - ``external`` — an inbound integration (server channels) whose *own*
#                  allowlist is the registration gate. Re-checking the signup
#                  patterns here would silently break every deployment where
#                  the two lists legitimately differ; see the long note on
#                  ``UserService.create_external_user``.
# - ``seed``     — first-superuser bootstrap, which must work on an instance
#                  that has no policy row yet.
_UNGATED_ORIGINS = frozenset(
    {
        AccountOrigin.ADMIN,
        AccountOrigin.INVITE,
        AccountOrigin.EXTERNAL,
        AccountOrigin.SEED,
    }
)

# Roles a new non-superuser account may be given. ``admin`` is absent by
# design: it is reserved for superusers and kept in sync with
# ``is_superuser``.
_ASSIGNABLE_DEFAULT_ROLES = (UserRole.USER.value, UserRole.DEVELOPER.value)


@dataclass(frozen=True)
class AccessPolicy:
    """The resolved front-door policy. Plain data, safe to pass anywhere."""

    registration_mode: str
    allowed_email_patterns: str
    password_auth_enabled: bool
    google_auto_register: bool
    default_user_role: str
    invite_include_desktop_default: bool
    # Derived from deployment settings, not from the admin row: Google sign-in
    # is only offered when a client id and secret are actually configured.
    google_auth_enabled: bool

    @property
    def registration_open(self) -> bool:
        """Only the literal ``"open"`` opens registration.

        An unknown mode — a newer deployment's value, a hand-edited row —
        degrades to closed. Failing the permissive branch narrow is what keeps
        an unrecognised string from silently becoming an open door.
        """
        return self.registration_mode == REGISTRATION_MODE_OPEN

    @property
    def password_signup_available(self) -> bool:
        """Whether this instance offers self-service password sign-up at all.

        The single answer to a question three surfaces ask — the login page
        (should it offer a "Sign up" link), the signup page (should it render
        the form) and `/start` (should it show a "Create account" button).
        Each of them used to recombine ``registration_open`` and
        ``password_auth_enabled`` for itself, and they drifted: the login page
        checked only the first, so a Google-only instance with open
        registration advertised a signup page that then refused to render.

        Deliberately **not** ``can_register``: that one also consults
        ``is_email_allowed``, which needs an address. This is "the door
        exists", which is what a projection with no viewer is allowed to say.
        Whether a given address may walk through it stays a server-side check
        at signup.

        It does not restate the gates either — it asks the same function
        ``can_register``'s SIGNUP branch asks. Removing the drift from the
        browser only to reintroduce it one layer down, as two expressions in
        this file with nothing keeping them in step, would be the same bug
        wearing a server-side coat.
        """
        return AccessPolicyService.signup_refusal_reason(self) is None


@dataclass(frozen=True)
class RegistrationDecision:
    """Whether an account may be created, and if not, why.

    ``reason`` is one of the ``REASON_*`` codes and is ``None`` exactly when
    ``allowed`` is True.
    """

    allowed: bool
    reason: str | None = None


class AccessPolicyValidationError(ValueError):
    """An admin update would violate a policy rule (usually a lockout).

    Carries the machine-readable ``reason`` code the route returns as the 400
    ``detail``, plus a human ``message`` for logs. A ``ValueError`` subclass so
    routes that already funnel domain errors keep working.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        self.message = message or reason
        super().__init__(reason)


class RegistrationNotAllowedError(ValueError):
    """Policy refused an account creation. ``str(exc)`` is the reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PasswordAuthDisabledError(ValueError):
    """This user may not use the password paths. ``str(exc)`` is the reason.

    A ``ValueError`` so services that already funnel domain errors to the
    route layer need no new except clause.
    """

    def __init__(self) -> None:
        self.reason = REASON_PASSWORD_AUTH_DISABLED
        super().__init__(REASON_PASSWORD_AUTH_DISABLED)


class AccessPolicyService:
    """Static resolver for the instance access policy."""

    # ── Resolution ─────────────────────────────────────────────────────

    @staticmethod
    def resolve(session: Session) -> AccessPolicy:
        """Read the policy row and merge in the settings-derived facts."""
        from app.services.server_config.server_config_service import (
            ServerConfigService,
        )

        config = ServerConfigService.get_or_create(session)
        return AccessPolicy(
            registration_mode=config.registration_mode,
            allowed_email_patterns=AccessPolicyService.normalize_email_patterns(
                config.allowed_email_patterns
            ),
            password_auth_enabled=config.password_auth_enabled,
            google_auto_register=config.google_auto_register,
            default_user_role=config.default_user_role,
            invite_include_desktop_default=config.invite_include_desktop_default,
            google_auth_enabled=settings.google_oauth_enabled,
        )

    @staticmethod
    def to_public(policy: AccessPolicy) -> AccessPolicyPublic:
        """Project the policy for anonymous readers (login / signup / landing).

        Patterns and the default role are omitted on purpose — see
        :class:`AccessPolicyPublic`.
        """
        return AccessPolicyPublic(
            registration_open=policy.registration_open,
            password_auth_enabled=policy.password_auth_enabled,
            google_auth_enabled=policy.google_auth_enabled,
            google_auto_register=policy.google_auto_register,
            # Gates the *advertisement* of the desktop client and nothing
            # else: `/start` hides its download card on this, while `/desktop`
            # renders unconditionally and `GET /desktop/download` is ungated,
            # so links in already-sent new-account emails keep working. Turning
            # `DESKTOP_AUTH_ENABLED` off therefore stops promoting the app; it
            # does not stop serving it.
            desktop_enabled=settings.DESKTOP_AUTH_ENABLED,
            project_name=settings.PROJECT_NAME,
            password_signup_available=policy.password_signup_available,
        )

    # ── Predicates ─────────────────────────────────────────────────────

    @staticmethod
    def normalize_email_patterns(patterns: str | None) -> str:
        """Canonical form of a comma-separated pattern list.

        Three readers ask "is this list empty?" and, left to themselves, three
        answers come back for ``" , "``: the validator skips blank entries and
        passes it, ``is_email_allowed`` sees a non-blank string and hands the
        matcher a list with no usable entries — refusing **everyone** — and the
        admin card counts its non-blank entries and renders "anyone can
        register". An admin who cleared the textarea imperfectly would get a
        silently closed instance that the UI reports as open.

        So "empty" is decided once, here: drop blank entries, strip the rest,
        rejoin with ``", "``. ``" , "`` becomes ``""`` and ``"*@acme.com,"``
        becomes ``"*@acme.com"``. Applied on write (``ServerConfigService.update``)
        so the stored value is canonical, and again in :meth:`resolve` so a row
        written before this existed is read the same way.
        """
        entries = [entry.strip() for entry in (patterns or "").split(",")]
        return ", ".join(entry for entry in entries if entry)

    @staticmethod
    def is_email_allowed(policy: AccessPolicy, email: str) -> bool:
        """Whether ``email`` may self-register under the pattern list.

        An empty list means "no restriction" — the one place the shared
        matcher's fail-closed default is inverted (see the module docstring).
        """
        patterns = (policy.allowed_email_patterns or "").strip()
        if not patterns:
            return True
        return match_email_pattern(email, patterns)

    @staticmethod
    def signup_refusal_reason(policy: AccessPolicy) -> str | None:
        """Why password self-registration is closed on this instance, or None.

        The **sole** encoding of the door-level SIGNUP gates: the ones that
        depend on the instance alone and not on who is knocking. Two readers
        need exactly this answer and must never encode it separately —
        ``can_register``'s SIGNUP branch, which then adds the per-address
        pattern check, and ``AccessPolicy.password_signup_available``, which is
        this returning ``None`` and is what the login, signup and landing pages
        render from.

        Returns a ``REASON_*`` code so the caller that owes the API a reason
        has one, and the caller that only needs a boolean can test for
        ``None``. A gate added here reaches both at once, which is the whole
        point: a third gate added to ``can_register`` alone would leave the
        Create-account button advertising a door the API refuses.
        """
        if not policy.registration_open:
            return REASON_REGISTRATION_CLOSED
        if not policy.password_auth_enabled:
            return REASON_PASSWORD_AUTH_DISABLED
        return None

    @staticmethod
    def can_register(
        session: Session, *, email: str, origin: AccountOrigin
    ) -> RegistrationDecision:
        """Whether an account may be created for ``email`` from ``origin``.

        ``admin`` / ``invite`` / ``external`` / ``seed`` always pass — those
        origins carry their own admission decision (see ``_UNGATED_ORIGINS``).
        ``signup`` needs open registration, password auth, and a pattern
        match. ``google`` needs open registration, ``google_auto_register``,
        and a pattern match — in invite-only mode Google never registers
        anyone, whatever ``google_auto_register`` says.
        """
        if origin in _UNGATED_ORIGINS:
            return RegistrationDecision(allowed=True)
        if origin not in (AccountOrigin.SIGNUP, AccountOrigin.GOOGLE):
            # A caller passing an origin nobody has reasoned about is a bug,
            # and guessing which gate it wanted is how a new path silently
            # skips the policy. Fail loudly instead.
            raise ValueError(f"Unknown registration origin: {origin!r}")

        policy = AccessPolicyService.resolve(session)

        if origin == AccountOrigin.SIGNUP:
            # The door-level gates live in one function, which
            # ``AccessPolicy.password_signup_available`` also reads — so the
            # button the browser renders and the answer this returns cannot
            # disagree, and a gate added there is added to both at once.
            refusal = AccessPolicyService.signup_refusal_reason(policy)
            if refusal is not None:
                return RegistrationDecision(allowed=False, reason=refusal)
        else:
            if not policy.registration_open:
                return RegistrationDecision(
                    allowed=False, reason=REASON_REGISTRATION_CLOSED
                )
            if not policy.google_auto_register:
                return RegistrationDecision(
                    allowed=False, reason=REASON_GOOGLE_AUTO_REGISTER_DISABLED
                )

        if not AccessPolicyService.is_email_allowed(policy, email):
            return RegistrationDecision(
                allowed=False, reason=REASON_EMAIL_NOT_ALLOWED
            )

        return RegistrationDecision(allowed=True)

    @staticmethod
    def is_password_auth_allowed(policy: AccessPolicy, user: User) -> bool:
        """Whether ``user`` may use the password paths.

        Superusers always may — with Google-only mode on, a broken or
        unreachable Google configuration would otherwise lock every
        administrator out of their own instance. That break-glass is the
        reason ``validate_update`` can afford to be strict about requiring a
        Google-linked admin before the switch is thrown.
        """
        return policy.password_auth_enabled or user.is_superuser

    @staticmethod
    def require_password_auth(session: Session, user: User) -> None:
        """Raise unless ``user`` may use the password paths.

        The gate exists once so the superuser break-glass cannot drift
        between login, set-password, change-password and reset. The recovery
        path deliberately does *not* use it: there, a refusal must be silent
        (see ``UserService.recover_password``).
        """
        policy = AccessPolicyService.resolve(session)
        if not AccessPolicyService.is_password_auth_allowed(policy, user):
            raise PasswordAuthDisabledError()

    @staticmethod
    def default_role(session: Session) -> str:
        """Role for a freshly created non-superuser account.

        Defends the never-``admin``-for-a-non-superuser invariant here as
        well: an out-of-range stored value degrades to ``agent-user`` rather
        than being handed on.
        """
        policy = AccessPolicyService.resolve(session)
        if policy.default_user_role in _ASSIGNABLE_DEFAULT_ROLES:
            return policy.default_user_role
        return UserRole.USER.value

    @staticmethod
    def can_change_email(session: Session) -> bool:
        """Whether users may edit their own email address.

        True only when no pattern list is configured. With one, a user could
        move themselves outside the allowlist the admin set — the address is
        the identity the policy is written against.
        """
        policy = AccessPolicyService.resolve(session)
        return not (policy.allowed_email_patterns or "").strip()

    @staticmethod
    def is_account_valid(user: User) -> bool:
        """Whether ``user`` may act on this request at all.

        Today: ``is_active``. It exists as a seam for the **browser-session
        path** — ``deps.get_current_user`` calls this instead of testing the
        flag inline, so a later per-request validity rule (suspension, expiry,
        licence) lands there without touching the check itself.

        It is deliberately NOT yet wired into the other token families that
        also test ``is_active``: the guest, agent-env, account-CLI and
        owner-resolution paths in ``deps``, the login routes' own inactive
        check, and ``AuthService.authenticate_with_google``. Those have
        different trust shapes and are switched over when there is a second
        rule to switch them for. No DB access; safe to call per request.
        """
        return user.is_active

    # ── Admin update validation ────────────────────────────────────────

    @staticmethod
    def validate_update(
        session: Session,
        current: ServerConfig,
        update: ServerConfigUpdate,
        acting_user: User,
    ) -> None:
        """Reject an admin update that is malformed or would lock everyone out.

        Two rules about *what* is validated, both there so an unrelated edit
        never fails on a control the admin was not touching:

        * **Shape** is checked only for fields actually submitted. A stored
          value that has drifted out of range — a hand-edited row, a mode a
          later release removed — must not block a disclaimer edit. Readers
          already degrade an unknown value conservatively.
        * **Lockout** is checked only on the *transition* into Google-only
          mode (password auth submitted false while stored true). An instance
          already in that state stays editable even if Google later becomes
          unconfigured: superusers keep the password break-glass, so the state
          is survivable, and refusing every future config edit would not be.

        Raises :class:`AccessPolicyValidationError`; the route maps
        ``.reason`` to a 400 detail.
        """
        submitted = {
            k: v
            for k, v in update.model_dump(exclude_unset=True).items()
            if v is not None
        }

        # ── Shape first: a malformed value is a typo, not a lockout, and
        # naming it is more useful than whatever lockout it also implies.
        if "registration_mode" in submitted:
            mode = submitted["registration_mode"]
            if mode not in VALID_REGISTRATION_MODES:
                raise AccessPolicyValidationError(
                    REASON_INVALID_REGISTRATION_MODE,
                    f"Registration mode must be one of "
                    f"{', '.join(VALID_REGISTRATION_MODES)}.",
                )

        if "default_user_role" in submitted:
            role = submitted["default_user_role"]
            if role not in _ASSIGNABLE_DEFAULT_ROLES:
                raise AccessPolicyValidationError(
                    REASON_INVALID_DEFAULT_USER_ROLE,
                    f"Default role must be one of "
                    f"{', '.join(_ASSIGNABLE_DEFAULT_ROLES)}.",
                )

        if "allowed_email_patterns" in submitted:
            bad_entry = AccessPolicyService._first_invalid_pattern(
                submitted["allowed_email_patterns"]
            )
            if bad_entry is not None:
                raise AccessPolicyValidationError(
                    f"{REASON_INVALID_EMAIL_PATTERN}:{bad_entry}",
                    f"'{bad_entry}' is not a valid email pattern. Use forms "
                    f"like *@acme.com or *@*.acme.com.",
                )

        # ── Lockout: only when password sign-in is being turned OFF here.
        turning_password_auth_off = (
            submitted.get("password_auth_enabled") is False
            and current.password_auth_enabled
        )
        if turning_password_auth_off:
            if not settings.google_oauth_enabled:
                raise AccessPolicyValidationError(
                    REASON_GOOGLE_OAUTH_NOT_CONFIGURED,
                    "Configure Google OAuth before turning off password "
                    "sign-in — nobody could sign in otherwise.",
                )
            if not AccessPolicyService._has_google_linked_admin(
                session, acting_user
            ):
                raise AccessPolicyValidationError(
                    REASON_NO_ADMIN_GOOGLE_ACCOUNT,
                    "Link a Google account to an administrator before turning "
                    "off password sign-in. Administrators keep password "
                    "sign-in as a break-glass path, but no admin here can "
                    "currently use Google.",
                )
            # The break-glass has to actually exist. ``is_password_auth_allowed``
            # returning True for a superuser is vacuous when that superuser has
            # no password hash — which is exactly the state
            # ``create_user_from_google`` leaves an admin who has only ever
            # signed in with Google. Without this check the sole Google-only
            # admin may throw the switch, and the day the Google client secret
            # rotates both doors are shut: Google rejects them, password login
            # has nothing to verify against, and recovery only works if SMTP
            # happens to be configured. The remedy is one call away
            # (``POST /users/me/set-password``), which is why this is a separate
            # code from the Google one — the two are separately actionable.
            if not AccessPolicyService._has_password_capable_admin(session):
                raise AccessPolicyValidationError(
                    REASON_NO_ADMIN_PASSWORD,
                    "Set a password on an administrator account before turning "
                    "off password sign-in. Administrators keep password "
                    "sign-in as a break-glass path for the day Google is "
                    "unreachable, and no administrator here has a password to "
                    "fall back on.",
                )

    @staticmethod
    def _first_invalid_pattern(patterns: str) -> str | None:
        """Return the first unusable entry in a comma-separated glob list.

        ``match_email_pattern`` itself rejects nothing — ``fnmatch`` happily
        compiles any string — so "malformed" has to be defined here. An entry
        is rejected when it could never match a real address:

        * it contains whitespace (a missing comma, the common paste mistake), or
        * it has no ``@`` and is not a pure wildcard.

        A bare ``acme.com`` is therefore refused: it looks like it should work
        and never matches anything, which is the worst possible failure for a
        security control. ``*`` is accepted and means "everyone", matching the
        channel whitelist's semantics.
        """
        for raw in (patterns or "").split(","):
            entry = raw.strip()
            if not entry:
                # Blank entries are normal — trailing commas are harmless.
                continue
            if any(ch.isspace() for ch in entry):
                return entry
            if "@" not in entry and set(entry) - {"*", "?"}:
                return entry
        return None

    @staticmethod
    def _has_google_linked_admin(session: Session, acting_user: User) -> bool:
        """Whether some administrator could sign in through Google."""
        if acting_user.is_superuser and acting_user.google_id:
            return True
        statement = select(User).where(
            User.is_superuser == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
            User.google_id != None,  # noqa: E711
        )
        return session.exec(statement).first() is not None

    @staticmethod
    def _has_password_capable_admin(session: Session) -> bool:
        """Whether some administrator could actually use the password break-glass.

        ``hashed_password`` is nullable: a Google-provisioned account has none,
        and for that user ``is_password_auth_allowed`` says yes to a door that
        opens onto nothing.
        """
        statement = select(User).where(
            User.is_superuser == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
            User.hashed_password != None,  # noqa: E711
        )
        return session.exec(statement).first() is not None

    # ── Startup ────────────────────────────────────────────────────────

    @staticmethod
    def warn_if_env_overrides_present() -> None:
        """Warn when the retired env settings are still set.

        They are seed-only from this release on: an operator who edits
        ``.env`` and restarts will see no behaviour change, so say where the
        live values now live. Never logs the values themselves.
        """
        retired = []
        if settings.AUTH_WHITELIST_USER_DOMAINS:
            retired.append("AUTH_WHITELIST_USER_DOMAINS")
        if settings.DEFAULT_USER_ROLE != UserRole.USER.value:
            retired.append("DEFAULT_USER_ROLE")
        if not retired:
            return
        logger.warning(
            "%s is set. It seeds the access policy only while no server_config "
            "row exists; once that row is created the database is the sole "
            "authority and further .env edits have no effect. Check and edit "
            "the live policy at /admin/server-configuration#access.",
            " and ".join(retired),
        )


__all__ = [
    "AccessPolicy",
    "AccessPolicyService",
    "AccessPolicyValidationError",
    "PasswordAuthDisabledError",
    "RegistrationDecision",
    "RegistrationNotAllowedError",
    "REASON_EMAIL_NOT_ALLOWED",
    "REASON_GOOGLE_AUTO_REGISTER_DISABLED",
    "REASON_GOOGLE_OAUTH_NOT_CONFIGURED",
    "REASON_INVALID_DEFAULT_USER_ROLE",
    "REASON_INVALID_EMAIL_PATTERN",
    "REASON_INVALID_REGISTRATION_MODE",
    "REASON_NO_ADMIN_GOOGLE_ACCOUNT",
    "REASON_NO_ADMIN_PASSWORD",
    "REASON_PASSWORD_AUTH_DISABLED",
    "REASON_REGISTRATION_CLOSED",
]
