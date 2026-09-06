import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import emails  # type: ignore
import jwt
from jinja2 import Template
from jwt.exceptions import InvalidTokenError

from sqlmodel import Session

from app.core import security
from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def restore_session(session: Session) -> None:
    """Make ``session`` usable again after a failed database operation.

    Unconditional, and the unconditionality is the whole content of this
    function. The obvious guard — ``if not session.is_active`` — detects only
    half the problem. A *flush* failure (an ``IntegrityError`` out of
    ``commit``) does deactivate the ``SessionTransaction``, so ``is_active``
    goes False. A *statement* failure (a ``select`` that errors, a lock
    timeout, a serialization failure, a connection dropped mid-statement)
    leaves Postgres' transaction aborted while SQLAlchemy's
    ``SessionTransaction`` is still nominally active, so ``is_active`` stays
    True and the guard skips exactly the case that most needs the rollback.
    The next ``commit`` on that session then dies with "current transaction is
    aborted" — inside whatever unrelated code happens to run next.

    Whether rolling back is *safe* is the caller's judgement, not this
    function's: it discards everything not yet committed. Callers use it where
    the only pending work belongs to the operation that just failed.

    Lives here rather than in either service that needs it because it is a
    pure session-lifecycle helper with no domain knowledge, and having two
    copies is how one of them ends up guarded on ``is_active`` again — see
    ``AccountProvisioningService`` and ``ManagedAICredentialsService.add_members``,
    which are the two ends of the same account-creation path.

    Never raises: it is called from failure handlers that have already been
    promised not to throw.
    """
    try:
        session.rollback()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to roll back the session after a failure.")


def as_utc(value: datetime) -> datetime:
    """Interpret a possibly-naive DB timestamp as UTC.

    The platform's timestamp columns are a mix: some are ``TIMESTAMP WITH TIME
    ZONE`` and read back aware, while older neighbours are ``TIMESTAMP WITHOUT
    TIME ZONE`` and read back naive — but every one of them is written from
    ``datetime.now(UTC)``, so a naive value is UTC wall-clock. Comparing the two
    kinds directly raises ``TypeError`` rather than failing quietly, and this is
    the shared reader that keeps that from happening.

    Lives here rather than in any one domain service because it is a pure
    datetime helper with no domain knowledge: bundle install bookkeeping and the
    status-repair sweep both need it, and neither should have to import the
    other to get it.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def create_task_with_error_logging(coro, task_name: str = "background_task"):
    """
    Create an asyncio task with proper exception logging.

    When using asyncio.create_task(), exceptions can be silently suppressed.
    This helper ensures exceptions are logged and tasks aren't prematurely cancelled.

    If called from a sync worker thread (no running event loop), the coroutine
    is scheduled on the main event loop via anyio.from_thread.

    Args:
        coro: Coroutine to run as a task
        task_name: Name for logging purposes

    Returns:
        asyncio.Task or None: The created task, or None if scheduled cross-thread
    """
    def _handle_task_result(task):
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info(f"Task {task_name} was cancelled")
        except Exception as e:
            logger.error(f"Unhandled exception in {task_name}: {e}", exc_info=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — called from a sync worker thread (e.g. anyio
        # run_sync).  Schedule the coroutine back on the main event loop.
        try:
            from anyio.from_thread import run as _anyio_run

            async def _schedule():
                t = asyncio.create_task(coro)
                t.add_done_callback(_handle_task_result)

            _anyio_run(_schedule)
        except Exception as e:
            logger.warning(f"Failed to schedule {task_name} from sync context: {e}")
            coro.close()
        return None

    task = asyncio.create_task(coro)
    task.add_done_callback(_handle_task_result)
    return task


@dataclass
class EmailData:
    html_content: str
    subject: str


def render_email_template(*, template_name: str, context: dict[str, Any]) -> str:
    template_str = (
        Path(__file__).parent / "email-templates" / "build" / template_name
    ).read_text()
    html_content = Template(template_str).render(context)
    return html_content


def send_email(
    *,
    email_to: str,
    subject: str = "",
    html_content: str = "",
) -> None:
    assert settings.emails_enabled, "no provided configuration for email variables"
    message = emails.Message(
        subject=subject,
        html=html_content,
        mail_from=(settings.EMAILS_FROM_NAME, settings.EMAILS_FROM_EMAIL),
    )
    smtp_options = {"host": settings.SMTP_HOST, "port": settings.SMTP_PORT}
    if settings.SMTP_TLS:
        smtp_options["tls"] = True
    elif settings.SMTP_SSL:
        smtp_options["ssl"] = True
    if settings.SMTP_USER:
        smtp_options["user"] = settings.SMTP_USER
    if settings.SMTP_PASSWORD:
        smtp_options["password"] = settings.SMTP_PASSWORD
    # Manage the SMTP backend explicitly instead of letting Message.send pull
    # one from the internal pool — the pooled backend keeps its socket open for
    # reuse and leaks it (ResourceWarning) until GC. Closing in a finally
    # guarantees the connection is released after every send.
    smtp_backend = emails.backend.SMTPBackend(**smtp_options)
    try:
        response = message.send(to=email_to, smtp=smtp_backend)
        logger.info(f"send email result: {response}")
    finally:
        smtp_backend.close()


def generate_test_email(email_to: str) -> EmailData:
    project_name = settings.PROJECT_NAME
    subject = f"{project_name} - Test email"
    html_content = render_email_template(
        template_name="test_email.html",
        context={"project_name": settings.PROJECT_NAME, "email": email_to},
    )
    return EmailData(html_content=html_content, subject=subject)


def generate_reset_password_email(email_to: str, email: str, token: str) -> EmailData:
    project_name = settings.PROJECT_NAME
    subject = f"{project_name} - Password recovery for user {email}"
    link = f"{settings.FRONTEND_HOST}/reset-password?token={token}"
    html_content = render_email_template(
        template_name="reset_password.html",
        context={
            "project_name": settings.PROJECT_NAME,
            "username": email,
            "email": email_to,
            "valid_hours": settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS,
            "link": link,
        },
    )
    return EmailData(html_content=html_content, subject=subject)


def generate_new_account_email(
    email_to: str, username: str, password: str
) -> EmailData:
    project_name = settings.PROJECT_NAME
    subject = f"{project_name} - New account for user {username}"
    # Primary call to action is the desktop landing page: it hands the user the
    # right build in one click and the desktop then bootstraps itself. The bare
    # SPA origin stays available as the secondary "just use the browser" link.
    link = f"{settings.FRONTEND_HOST}/desktop"
    web_link = settings.FRONTEND_HOST
    html_content = render_email_template(
        template_name="new_account.html",
        context={
            "project_name": settings.PROJECT_NAME,
            "username": username,
            "password": password,
            "email": email_to,
            "link": link,
            "web_link": web_link,
        },
    )
    return EmailData(html_content=html_content, subject=subject)


def generate_confirmation_email(email_to: str, email: str, token: str) -> EmailData:
    project_name = settings.PROJECT_NAME
    subject = f"{project_name} - Confirm your email address"
    link = f"{settings.FRONTEND_HOST}/confirm-email?token={token}"
    html_content = render_email_template(
        template_name="confirm_email.html",
        context={
            "project_name": settings.PROJECT_NAME,
            "username": email,
            "email": email_to,
            "valid_hours": settings.EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS,
            "link": link,
        },
    )
    return EmailData(html_content=html_content, subject=subject)


def generate_password_reset_token(email: str) -> str:
    delta = timedelta(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS)
    now = datetime.now(timezone.utc)
    expires = now + delta
    exp = expires.timestamp()
    encoded_jwt = jwt.encode(
        {"exp": exp, "nbf": now, "sub": email},
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )
    return encoded_jwt


def verify_password_reset_token(token: str) -> str | None:
    """The address a password-reset token names, or ``None``.

    THE PURPOSE CHECK IS A REJECTION, NOT A REQUIREMENT
    ---------------------------------------------------
    A reset token carries no ``purpose`` claim and — for backward
    compatibility with links already in people's inboxes — never will, so this
    cannot require one the way ``verify_email_confirmation_token`` does. What
    it can do is refuse every token that carries one, because a ``purpose``
    claim means the token was minted for a *different* flow and that flow's
    own verifier is the only one entitled to accept it.

    Without that refusal this function accepts any HS256 token signed with
    ``SECRET_KEY`` that has a ``sub``, which includes the invitation token.
    The consequences are not theoretical: an invitee could replay their invite
    link at ``POST /reset-password/`` and set a password there, so revoking
    the invitation would stop nothing, resend's ``jti`` rotation would
    invalidate nothing, and the single-use property that makes an accepted
    invitation terminal would be lost — a leaked invite link would be a
    week-long account-takeover primitive.
    """
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        return None
    if "purpose" in decoded_token:
        return None
    sub = decoded_token.get("sub")
    if not sub:
        return None
    return str(sub)


# ── Email confirmation tokens ───────────────────────────────────────────
# Parallel to the password-reset token pair, but with a distinct
# ``purpose`` claim so a reset token can never be replayed as a confirm
# token (and vice versa). ``verify_email_confirmation_token`` *requires*
# the purpose claim; password-reset tokens carry none and are rejected.
_EMAIL_CONFIRM_PURPOSE = "email_confirm"


def generate_email_confirmation_token(email: str) -> str:
    delta = timedelta(hours=settings.EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS)
    now = datetime.now(timezone.utc)
    expires = now + delta
    exp = expires.timestamp()
    encoded_jwt = jwt.encode(
        {"exp": exp, "nbf": now, "sub": email, "purpose": _EMAIL_CONFIRM_PURPOSE},
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )
    return encoded_jwt


def verify_email_confirmation_token(token: str) -> str | None:
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        return None
    # Reject any token lacking the confirmation purpose (e.g. a
    # password-reset token) so token types cannot cross-pollinate.
    if decoded_token.get("purpose") != _EMAIL_CONFIRM_PURPOSE:
        return None
    return str(decoded_token["sub"])


def generate_invitation_email(
    *,
    email_to: str,
    full_name: str | None,
    accept_link: str,
    web_link: str,
    desktop_link: str | None,
    auth_hint: str,
    invited_by_name: str | None,
    expires_in_days: int,
) -> EmailData:
    """Render the invitation email.

    ``auth_hint`` here is the **effective** hint — the one
    ``InvitationService`` resolves against the access policy at send time, not
    the one stored on the row. That matters on resend: an instance that has
    since been switched to Google-only must not send a fresh email telling the
    person to choose a password they will not be allowed to set.

    ``desktop_link`` is ``None`` when the invitation does not include the
    desktop block, so the template branches on presence rather than on a second
    boolean that could disagree with it.
    """
    project_name = settings.PROJECT_NAME
    subject = f"{project_name} - You have been invited"
    html_content = render_email_template(
        template_name="invite.html",
        context={
            "project_name": project_name,
            "email": email_to,
            "username": full_name or email_to,
            "link": accept_link,
            "web_link": web_link,
            "desktop_link": desktop_link,
            "auth_hint": auth_hint,
            "invited_by_name": invited_by_name,
            "valid_days": expires_in_days,
        },
    )
    return EmailData(html_content=html_content, subject=subject)


# ── Invitation tokens ───────────────────────────────────────────────────
# The confirmation-token pattern, not the reset-token one, and the choice is
# load-bearing. ``generate_password_reset_token`` emits no ``purpose`` claim at
# all, so *any* purposeless HS256 token signed with ``SECRET_KEY`` validates as
# a reset token. An invitation grants an account, so it is stamped and the
# stamp is checked before anything else.
#
# The addition over the confirmation token is ``jti``: the invitation is loaded
# **by that claim**, never by ``sub``. Resending rotates ``token_jti`` on the
# row, which is what makes a previously emailed link stop working — resolving
# by address instead would leave every old link live and make revocation
# decorative.
_INVITE_PURPOSE = "invite"


def generate_invitation_token(*, email: str, jti: str, expires_at: datetime) -> str:
    """Mint the token an invitation email carries.

    ``expires_at`` is the invitation row's own column, so the link and the row
    expire at the same instant by construction rather than by two settings
    agreeing.

    ``jti`` must come from the **committed** row. A token minted from a jti
    that is never persisted (or is superseded before the commit lands) passes
    signature and purpose verification and then fails the row lookup — which
    is indistinguishable, by design, from a forgery. The recipient is told the
    invitation is no longer valid and no log says why.
    """
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    encoded_jwt = jwt.encode(
        {
            "exp": expires_at.timestamp(),
            "nbf": now,
            "sub": email,
            "jti": jti,
            "purpose": _INVITE_PURPOSE,
        },
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )
    return encoded_jwt


def verify_invitation_token(token: str) -> tuple[str, str] | None:
    """``(email, jti)`` for a well-formed, unexpired invitation token.

    Returns ``None`` — never raises, never distinguishes — for a malformed
    token, a bad signature, an expired one, and a token of any other purpose.
    The caller turns every one of those into the same answer it gives for a
    jti that is not in the table.

    Both halves are returned because both are used, for different things: the
    ``jti`` *resolves* the invitation, and the address is then compared against
    the resolved user's own as an independent second check, so an admin who
    changed the address after inviting invalidates the outstanding link.
    """
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        return None
    # Purpose first: a purposeless password-reset token would otherwise
    # validate here, and it is signed with the same key.
    if decoded_token.get("purpose") != _INVITE_PURPOSE:
        return None
    email = decoded_token.get("sub")
    jti = decoded_token.get("jti")
    if not email or not jti:
        return None
    return str(email), str(jti)


def get_base_url(request) -> str:
    """Extract base URL from a request, respecting X-Forwarded-Proto for reverse proxies."""
    url = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def client_ip(request) -> str | None:
    """Best-effort source IP for audit records.

    Prefers the first ``X-Forwarded-For`` hop (the real client when the app sits
    behind nginx / a load balancer) and falls back to the socket peer. Truncated
    to 64 chars to match the audit columns' cap. ``None`` in, ``None`` out — an
    audit written from a non-HTTP context still has an IP field, just an empty one.

    Canonical implementation: ``account_cli_service``, ``account_api_proxy_service``,
    ``device_login_service`` and ``agent_hooks`` all call this one — do not add
    another private copy, or audit IPs drift apart between transports.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


def detect_anthropic_credential_type(api_key: str) -> tuple[str, str]:
    """
    Detect the type of Anthropic credential based on its prefix.

    Args:
        api_key: The Anthropic API key or OAuth token

    Returns:
        Tuple of (env_var_name, key_type_description)
        - sk-ant-oat* → ("CLAUDE_CODE_OAUTH_TOKEN", "OAuth Token")
        - sk-ant-api* → ("ANTHROPIC_API_KEY", "API Key")
        - other → ("ANTHROPIC_API_KEY", "API Key (Unknown Format)")

    Examples:
        >>> detect_anthropic_credential_type("sk-ant-oat01-abc123")
        ("CLAUDE_CODE_OAUTH_TOKEN", "OAuth Token")

        >>> detect_anthropic_credential_type("sk-ant-api03-xyz789")
        ("ANTHROPIC_API_KEY", "API Key")
    """
    if not api_key:
        return ("ANTHROPIC_API_KEY", "API Key (Empty)")

    # OAuth tokens start with sk-ant-oat
    if api_key.startswith("sk-ant-oat"):
        return ("CLAUDE_CODE_OAUTH_TOKEN", "OAuth Token")

    # API keys start with sk-ant-api
    if api_key.startswith("sk-ant-api"):
        return ("ANTHROPIC_API_KEY", "API Key")

    # Unknown format defaults to API key
    return ("ANTHROPIC_API_KEY", "API Key (Unknown Format)")
