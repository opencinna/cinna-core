import uuid
from datetime import UTC, datetime

from sqlalchemy import Text
from sqlmodel import Field, SQLModel

# ── Registration modes ─────────────────────────────────────────────────
#
# Plain strings on a VARCHAR column rather than a DB enum: adding a mode
# later must not need a migration, and every reader degrades the same way
# (only the literal ``"open"`` opens registration — see
# ``AccessPolicy.registration_open``).
REGISTRATION_MODE_OPEN = "open"
REGISTRATION_MODE_INVITE_ONLY = "invite_only"
VALID_REGISTRATION_MODES = (REGISTRATION_MODE_OPEN, REGISTRATION_MODE_INVITE_ONLY)


class ServerConfig(SQLModel, table=True):
    """
    Singleton server-wide configuration.

    Only one row ever exists; it is created lazily on first access. Holds the
    admin-configurable disclaimer settings shown to users at login and the
    instance-level switches for public surfaces.
    """
    __tablename__ = "server_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Disclaimer settings
    disclaimer_enabled: bool = Field(default=False)
    disclaimer_markdown: str = Field(default="", sa_type=Text)
    # "new_users" -> show once per browser; "every_login" -> show once per session
    disclaimer_display_mode: str = Field(default="new_users")
    # Bumped on every content/mode change so acknowledged users re-see edits.
    disclaimer_version: int = Field(default=1)

    # Local Agent Kit: whether this instance publishes the public, auth-free
    # `/agent-start` surface. Opt-out, so a fresh instance ships the kit.
    local_agent_kit_enabled: bool = Field(default=True)

    # ── Access policy ──────────────────────────────────────────────────
    #
    # Who may get an account on this instance, and how they sign in. These
    # six columns replace the ``AUTH_WHITELIST_USER_DOMAINS`` and
    # ``DEFAULT_USER_ROLE`` env settings, which survive only as the
    # first-boot seed (see ``ServerConfigService.get_or_create``).
    #
    # Nothing here participates in ``disclaimer_version`` — an access-policy
    # edit must never force every user to re-acknowledge the disclaimer.
    #
    # Resolved for readers by ``AccessPolicyService``; never re-derived
    # anywhere else.

    # "open" or "invite_only". Gates *registration* only: an existing user
    # is never locked out by flipping this.
    registration_mode: str = Field(default=REGISTRATION_MODE_OPEN, max_length=16)

    # Comma-separated fnmatch globs in the ``email_patterns.py`` syntax
    # (``*@acme.com, *@*.acme.com``). Empty means "no restriction" — the
    # inverse of the channel whitelist's fail-closed default, because this
    # list gates *self-registration*, and an empty one here is the
    # backward-compatible "anyone may sign up" the platform shipped with.
    allowed_email_patterns: str = Field(default="", sa_type=Text)

    # When false, non-superusers cannot use password login, signup,
    # recovery, reset or set-password. Superusers keep password login as
    # the break-glass path — see ``AccessPolicyService.is_password_auth_allowed``.
    password_auth_enabled: bool = Field(default=True)

    # Whether a Google login on an unknown email may create an account.
    # Ignored in invite-only mode, where Google never registers anyone.
    google_auto_register: bool = Field(default=True)

    # Role given to new non-superuser accounts. Constrained to
    # ``agent-user`` / ``agent-developer`` at validation; ``admin`` is
    # rejected so the role ⇔ is_superuser invariant survives.
    default_user_role: str = Field(default="agent-user", max_length=32)

    # Pre-ticks the "include desktop instructions" checkbox in the
    # invitation wizard. Presentation default only — no security meaning.
    invite_include_desktop_default: bool = Field(default=True)

    # Audit
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )


class ServerConfigUpdate(SQLModel):
    """Admin update payload — all fields optional."""
    disclaimer_enabled: bool | None = None
    disclaimer_markdown: str | None = None
    disclaimer_display_mode: str | None = None
    local_agent_kit_enabled: bool | None = None
    # Access policy — validated by ``AccessPolicyService.validate_update``
    # before anything is applied (lockout prevention is server-side).
    registration_mode: str | None = None
    allowed_email_patterns: str | None = None
    password_auth_enabled: bool | None = None
    google_auto_register: bool | None = None
    default_user_role: str | None = None
    invite_include_desktop_default: bool | None = None


class DisclaimerPublic(SQLModel):
    """Disclaimer projection returned to any authenticated user."""
    enabled: bool
    markdown: str
    display_mode: str
    version: int


class AccessPolicyPublic(SQLModel):
    """What an anonymous visitor is told about this instance's front door.

    Deliberately excludes ``allowed_email_patterns`` and
    ``default_user_role``. Both describe *who* gets in and *what they
    become* — customer domains and role policy are not for anonymous
    readers, and the login page never needs them to decide what to render.

    This projection says what the **instance offers**, never what a
    particular person may do: there is no viewer here to gate on.
    """
    registration_open: bool
    password_auth_enabled: bool
    google_auth_enabled: bool
    google_auto_register: bool
    desktop_enabled: bool
    project_name: str
