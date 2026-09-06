import re
import uuid
from datetime import UTC, datetime

from pydantic import field_validator
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

# Ceiling on the admin-authored landing copy. The first length cap on any
# markdown column here, and it exists because this is the first one served to
# **anonymous** callers: ``GET /server-config/landing`` is reachable without a
# token, so an uncapped blob would be an amplification primitive keyed on
# nothing but a rate limit. Declared once and enforced once — on
# ``ServerConfigUpdate``, at the API boundary — so there is no second opinion
# about how long "too long" is.
LANDING_MARKDOWN_MAX_LENGTH = 16 * 1024


# ── Raw HTML in the landing copy ───────────────────────────────────────
#
# `/start` renders this column with `MarkdownRenderer`, where raw HTML is
# inert *only* because `react-markdown` does not parse it without
# `rehype-raw`. That is a default, not a configuration — so the day someone
# adds that plugin for an unrelated chat feature, every landing page already
# stored becomes markup served to anonymous visitors, in a diff that never
# touches `/start`.
#
# The pin therefore lives here, on the write path, where the API test suite
# can see it: nothing HTML-shaped gets *into* the column in the first place,
# whatever the renderer is later configured to do. Markdown needs no raw HTML
# to say anything an admin welcome message wants to say, so refusing it costs
# nothing real.
#
# Deliberately conservative about false positives — a rule that refuses an
# admin's code sample is a bug, not caution:
#   * `<` must be followed immediately by a letter or `/!?` to read as a tag,
#     so prose comparisons (`a < b`, `x > y`) pass;
#   * fenced blocks and inline code spans are excluded before the check,
#     because Markdown turns their contents into a `code` element as *text* —
#     escaped by the parser, and still escaped with a raw-HTML plugin on.
#     Indented (four-space) code blocks are NOT excluded: telling them apart
#     from a list item's continuation line needs a real block parser, and
#     over-excluding here is the direction that hides markup. The refusal
#     message points the admin at a fence, which does work.
#
# Recognising a fence *loosely* is the failure that matters: everything from a
# bogus opener to the end of the document would be dropped unchecked. So this
# follows CommonMark — at most three spaces of indent (four makes it an
# indented code block, not a fence), and a backtick fence's info string may
# not itself contain a backtick (``` `x` ``` on one line is a paragraph with
# a code span, not an opener).
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Single-line code spans only. A CommonMark code span may wrap a line, but
# recognising that correctly needs the block structure this function does not
# have — and a run that crosses lines is exactly how a stray backtick either
# side of a `<script>` would blank it out of the check while the renderer
# still emitted it as markup. Refusing to span lines errs toward rejecting a
# multi-line code span, never toward hiding a tag.
_INLINE_CODE_RE = re.compile(r"(`+)[^\n]*?\1")
# `<` immediately followed by a tag name, a closing slash, `!` (comments,
# doctype, CDATA) or `?` (processing instruction) — the four shapes an HTML
# parser treats as markup.
_HTML_TAG_RE = re.compile(r"<[!?/]|<[a-zA-Z][a-zA-Z0-9-]*[\s/>]")
# Quoted `on…=` attribute values. Unreachable without a tag in practice; kept
# so the reason a payload is refused is named rather than incidental.
_EVENT_HANDLER_RE = re.compile(r"(?<![\w-])on[a-z]{3,}\s*=\s*[\"']", re.IGNORECASE)
# Script-bearing URL schemes, required to be followed by a non-space so a
# sentence like "JavaScript: enabled" is not mistaken for a link target.
_DANGEROUS_SCHEME_RE = re.compile(
    r"(?:javascript|vbscript):\S|data:\s*text/html", re.IGNORECASE
)


def _strip_code_regions(markdown: str) -> str:
    """Blank out fenced code blocks and inline code spans.

    What survives is the text that a Markdown renderer would emit as prose —
    the only place raw HTML could ever become an element.
    """
    kept: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                continue
            kept.append(line)
        elif (
            match
            # A closing fence is the same character, at least as long, and
            # carries no info string of its own.
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not match.group(2).strip()
        ):
            fence = None
    return _INLINE_CODE_RE.sub(" ", "\n".join(kept))


def reject_raw_html(markdown: str) -> None:
    """Raise ``ValueError`` if *markdown* carries HTML-shaped content.

    See the block comment above for why this is enforced on the write path
    rather than trusted to the renderer's defaults.
    """
    prose = _strip_code_regions(markdown)
    for pattern, what in (
        (_HTML_TAG_RE, "an HTML tag"),
        (_EVENT_HANDLER_RE, "an HTML event-handler attribute"),
        (_DANGEROUS_SCHEME_RE, "a script-bearing URL scheme"),
    ):
        if pattern.search(prose):
            raise ValueError(
                f"landing_markdown looks like it contains {what}. This copy is "
                "served to anonymous visitors on the public landing page and is "
                "rendered as Markdown only — raw HTML is not accepted. Use "
                "Markdown syntax, or put the example inside a code block."
            )


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

    # Admin-authored welcome copy for the public `/start` landing page,
    # rendered as Markdown. Empty means the page simply omits the block.
    #
    # Not part of the disclaimer: it is never acknowledged, so it must never
    # touch ``disclaimer_version`` (see ``ServerConfigService.update``, whose
    # bump is an explicit two-field check). And not part of the access-policy
    # projection: this is *content*, not front-door policy — it is served by
    # its own public endpoint so the login and signup pages, which share the
    # policy query, never download a landing page they do not render.
    landing_markdown: str = Field(default="", sa_type=Text)

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


# Both rules on ``landing_markdown`` — the 16 KiB cap and the raw-HTML
# refusal — live on the field below and nowhere else, so the class is their
# enforcement boundary. That holds only because ``PUT /admin/server-config``
# is the sole writer today. A future non-API writer — a service assigning
# ``config.landing_markdown``, a data migration, a seed — would bypass both
# and put uncapped, unvalidated content into a column that
# ``GET /server-config/landing`` serves to anonymous callers. Any such writer
# must route through this model, or call :func:`reject_raw_html` and check
# the length itself.
class ServerConfigUpdate(SQLModel):
    """Admin update payload — all fields optional, and the only enforcement
    point for the ``landing_markdown`` length cap and raw-HTML refusal.
    """
    disclaimer_enabled: bool | None = None
    disclaimer_markdown: str | None = None
    disclaimer_display_mode: str | None = None
    local_agent_kit_enabled: bool | None = None
    # ``None`` means "not being changed"; ``""`` means "clear the welcome
    # copy". Both have to stay expressible, so the cap lives on the field
    # rather than in a write-site check that would have to re-decide which of
    # the two an absent key was.
    landing_markdown: str | None = Field(
        default=None, max_length=LANDING_MARKDOWN_MAX_LENGTH
    )
    # Access policy — validated by ``AccessPolicyService.validate_update``
    # before anything is applied (lockout prevention is server-side).
    registration_mode: str | None = None
    allowed_email_patterns: str | None = None
    password_auth_enabled: bool | None = None
    google_auto_register: bool | None = None
    default_user_role: str | None = None
    invite_include_desktop_default: bool | None = None

    @field_validator("landing_markdown")
    @classmethod
    def _no_raw_html(cls, value: str | None) -> str | None:
        """Refuse HTML-shaped landing copy at the API boundary.

        Validation only — the stored value is unchanged, so this needs no
        migration and does not re-interpret ``None`` (not being changed) or
        ``""`` (clear the copy), neither of which reaches the check.
        """
        if value:
            reject_raw_html(value)
        return value


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
    # The *answer*, not the ingredients. "May someone self-register with a
    # password here?" is ``registration_open and password_auth_enabled``, and
    # three surfaces need it (/login, /signup, /start). Recombining the two
    # facts client-side is how they drifted: the login page offered a Sign up
    # link on a password-auth-off instance and the signup page then refused to
    # render the form. Resolved once, server-side, in
    # ``AccessPolicy.password_signup_available``.
    #
    # This is still "what the instance offers", never "what you may do": a
    # particular address may still fail the pattern list at signup, which stays
    # a server-side check with no viewer to gate on here.
    password_signup_available: bool


class LandingPagePublic(SQLModel):
    """The admin-authored welcome copy for the public `/start` page.

    Its own projection, and its own endpoint, because it is content rather
    than policy: :class:`AccessPolicyPublic` is fetched by every login and
    signup page load under one shared cache key, and none of them render this.
    """

    landing_markdown: str
