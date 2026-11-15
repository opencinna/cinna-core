"""Browser (web SPA) refresh sessions: create, rotate, revoke, clean up.

A web session is opt-in per login (``X-Cinna-Web-Session: 1``). It owns a
rotating opaque refresh token (delivered only as the ``cinna_refresh``
HttpOnly cookie) and a CSRF token fixed for the session's life (returned once
in the login body). Only SHA-256 hashes of both are stored.

No HTTP types here: the cookie, Origin and status mapping live in
``app.api.routes._web_session_http``.
"""
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.models import User, UserWebRefreshToken, UserWebSession
from app.services.desktop_auth.desktop_auth_crypto import (
    generate_refresh_token,
    hash_token,
)
from app.services.users.access_policy_service import AccessPolicyService

logger = logging.getLogger(__name__)

# Stored ``user_agent`` is display-only; the column is VARCHAR(512).
_USER_AGENT_MAX_CHARS = 512

# Ended sessions (expired or revoked) are kept this long before cleanup.
_CLEANUP_RETENTION = timedelta(days=1)

# ``WebSessionError`` codes.
ERROR_SESSION_EXPIRED = "session_expired"
ERROR_CSRF_FAILED = "csrf_failed"


class WebSessionError(Exception):
    """A refresh/logout that cannot proceed. ``code`` is the public detail."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RotateResult:
    user: User
    web_session: UserWebSession
    # ``None`` inside the reuse-grace window: mint an access token only, the
    # cookie stays as it is.
    new_refresh_raw: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp read back from the DB as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _refresh_lifetime() -> timedelta:
    return timedelta(days=settings.WEB_REFRESH_TOKEN_EXPIRE_DAYS)


def _csrf_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class WebSessionService:
    @staticmethod
    def create(
        session: Session, user: User, user_agent: str | None
    ) -> tuple[UserWebSession, str, str]:
        """Open a web session for ``user`` and commit it.

        Returns ``(web_session, refresh_raw, csrf_raw)``; the raw values are
        never stored and must be handed to the client now.
        """
        now = _now()
        expires_at = now + _refresh_lifetime()
        csrf_raw = secrets.token_urlsafe(32)
        refresh_raw = generate_refresh_token()
        web_session = UserWebSession(
            user_id=user.id,
            csrf_token_hash=_csrf_hash(csrf_raw),
            user_agent=(user_agent or None) and user_agent[:_USER_AGENT_MAX_CHARS],
            created_at=now,
            last_used_at=now,
            expires_at=expires_at,
        )
        session.add(web_session)
        session.flush()
        session.add(
            UserWebRefreshToken(
                session_id=web_session.id,
                token_hash=hash_token(refresh_raw),
                expires_at=expires_at,
                created_at=now,
            )
        )
        session.commit()
        session.refresh(web_session)
        return web_session, refresh_raw, csrf_raw

    @staticmethod
    def verify_csrf(web_session: UserWebSession, header: str | None) -> bool:
        """Constant-time comparison of the presented CSRF token's hash."""
        if not header:
            return False
        return hmac.compare_digest(
            _csrf_hash(header), web_session.csrf_token_hash
        )

    @staticmethod
    def find_live_session(
        session: Session, refresh_raw: str | None
    ) -> UserWebSession | None:
        """The live (not revoked, not expired) session owning ``refresh_raw``,
        whatever the token's own rotation state. Read-only."""
        if not refresh_raw:
            return None
        row = session.exec(
            select(UserWebRefreshToken).where(
                UserWebRefreshToken.token_hash == hash_token(refresh_raw)
            )
        ).first()
        if row is None:
            return None
        web_session = session.get(UserWebSession, row.session_id)
        if web_session is None or web_session.revoked_at is not None:
            return None
        if _aware(web_session.expires_at) <= _now():
            return None
        return web_session

    @classmethod
    def rotate(
        cls, session: Session, refresh_raw: str | None, *, csrf_token: str | None
    ) -> RotateResult:
        """Exchange a refresh token for a successor and commit.

        The CSRF token is checked against the resolved session before
        anything is written, so a failed check never consumes the token.

        Raises ``WebSessionError("session_expired")`` for an unknown, expired
        or revoked token or session, an invalid user, or a replay outside the
        grace window (which also revokes the session); ``"csrf_failed"`` for
        a CSRF mismatch.
        """
        if not refresh_raw:
            raise WebSessionError(ERROR_SESSION_EXPIRED)
        now = _now()
        token_row = session.exec(
            select(UserWebRefreshToken).where(
                UserWebRefreshToken.token_hash == hash_token(refresh_raw)
            )
        ).first()
        if token_row is None:
            raise WebSessionError(ERROR_SESSION_EXPIRED)
        # Lock the web session row, then re-read the token under it (the same
        # session -> token order as the cleanup cascade). Two tabs refreshing
        # with one token serialise here, and the second lands in the grace
        # branch instead of minting a second successor. ``end_sessions``'
        # revoking UPDATE serialises here too: if it ran first, ``revoked_at``
        # is seen below; if it runs after, it revokes what this call wrote.
        web_session = session.exec(
            select(UserWebSession)
            .where(UserWebSession.id == token_row.session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        token_row = session.exec(
            select(UserWebRefreshToken)
            .where(UserWebRefreshToken.id == token_row.id)
            .execution_options(populate_existing=True)
        ).first()
        if (
            token_row is None
            or web_session is None
            or web_session.revoked_at is not None
            or _aware(web_session.expires_at) <= now
            or _aware(token_row.expires_at) <= now
        ):
            session.rollback()
            raise WebSessionError(ERROR_SESSION_EXPIRED)

        user = session.get(User, web_session.user_id)
        if user is None or not AccessPolicyService.is_account_valid(user):
            session.rollback()
            raise WebSessionError(ERROR_SESSION_EXPIRED)

        if not cls.verify_csrf(web_session, csrf_token):
            session.rollback()
            raise WebSessionError(ERROR_CSRF_FAILED)

        if token_row.rotated_at is not None:
            grace = timedelta(seconds=settings.WEB_REFRESH_TOKEN_REUSE_GRACE_SECONDS)
            if now - _aware(token_row.rotated_at) <= grace:
                # Lost-response retry or a concurrent tab: no second rotation,
                # so the shared cookie jar is never overwritten with a token
                # this call would then have to revoke.
                web_session.last_used_at = now
                session.add(web_session)
                session.commit()
                cls._reload_live(session, user, web_session)
                return RotateResult(user, web_session, None)
            # Replay outside grace: treat as theft, end the whole session.
            web_session.revoked_at = now
            session.add(web_session)
            session.commit()
            logger.warning(
                "Web refresh replay outside grace; revoked web session %s",
                web_session.id,
            )
            raise WebSessionError(ERROR_SESSION_EXPIRED)

        new_raw = generate_refresh_token()
        expires_at = now + _refresh_lifetime()
        token_row.rotated_at = now
        session.add(token_row)
        session.add(
            UserWebRefreshToken(
                session_id=web_session.id,
                token_hash=hash_token(new_raw),
                expires_at=expires_at,
                created_at=now,
            )
        )
        web_session.expires_at = expires_at
        web_session.last_used_at = now
        session.add(web_session)
        session.commit()
        cls._reload_live(session, user, web_session)
        return RotateResult(user, web_session, new_raw)

    @staticmethod
    def _reload_live(
        session: Session, user: User, web_session: UserWebSession
    ) -> None:
        """Reload ``user`` then ``web_session`` after the rotate commit.

        The caller mints the access token from ``user.session_version`` as
        read here. ``end_sessions`` bumps ``sv`` and revokes web sessions in
        one transaction, so if the user read sees a bumped ``sv``, the later
        web session read sees the revocation and the refresh is refused. A
        session that ``end_sessions`` kept stays live and gets the new ``sv``.
        """
        session.refresh(user)
        session.refresh(web_session)
        if web_session.revoked_at is not None:
            raise WebSessionError(ERROR_SESSION_EXPIRED)

    @staticmethod
    def revoke(session: Session, web_session_id: uuid.UUID) -> None:
        """Revoke one web session and commit. Idempotent."""
        session.execute(
            update(UserWebSession)
            .where(
                UserWebSession.id == web_session_id,
                UserWebSession.revoked_at.is_(None),  # type: ignore[union-attr]
            )
            .values(revoked_at=_now())
        )
        session.commit()

    @staticmethod
    def revoke_all_for_user(
        session: Session,
        user_id: uuid.UUID,
        except_id: uuid.UUID | None = None,
    ) -> None:
        """Revoke every live web session of the user except ``except_id``.

        Does NOT commit: called from ``UserSessionService.end_sessions``,
        whose caller commits.
        """
        stmt = update(UserWebSession).where(
            UserWebSession.user_id == user_id,
            UserWebSession.revoked_at.is_(None),  # type: ignore[union-attr]
        )
        if except_id is not None:
            stmt = stmt.where(UserWebSession.id != except_id)
        session.execute(stmt.values(revoked_at=_now()))

    @staticmethod
    def cleanup_expired(session: Session) -> int:
        """Delete sessions that ended (expired or revoked) over a day ago.

        Their refresh tokens go with them (``ON DELETE CASCADE``). Returns
        the number of sessions removed.
        """
        cutoff = _now() - _CLEANUP_RETENTION
        result = session.exec(
            delete(UserWebSession).where(
                or_(
                    UserWebSession.expires_at < cutoff,  # type: ignore[arg-type]
                    UserWebSession.revoked_at < cutoff,  # type: ignore[arg-type,operator]
                )
            )
        )
        session.commit()
        return int(result.rowcount or 0)
