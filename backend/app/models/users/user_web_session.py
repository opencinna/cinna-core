"""Browser (web SPA) refresh sessions.

One ``UserWebSession`` row per signed-in browser that opted into a cookie
session; its refresh tokens rotate in ``UserWebRefreshToken``. Kept apart
from ``desktop_refresh_token`` on purpose: a desktop client row is part of the
native contract (listed verbatim by ``/desktop-auth/clients``), and browser
sessions must not appear there.

No ``Public`` variants: no API exposes these rows. Only SHA-256 hashes of the
refresh token and the CSRF token are stored, never the plaintext.
``WebSessionToken`` is the response of ``POST /login/session/refresh``.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class UserWebSession(SQLModel, table=True):
    """A browser session. ``revoked_at IS NULL`` means live."""

    __tablename__ = "user_web_session"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    # SHA-256 hex of the session's CSRF token (fixed for the session's life).
    csrf_token_hash: str = Field(max_length=64)
    # Truncated User-Agent captured at session creation. Display-only.
    user_agent: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))
    last_used_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class UserWebRefreshToken(SQLModel, table=True):
    """One rotating refresh token of a web session.

    ``rotated_at`` is set when the token is superseded by a rotation; it is
    the discriminator for the reuse-grace window.
    """

    __tablename__ = "user_web_refresh_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(
        foreign_key="user_web_session.id", ondelete="CASCADE", index=True
    )
    # SHA-256 hex of the opaque refresh token.
    token_hash: str = Field(max_length=64, unique=True)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    rotated_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))


class WebSessionToken(SQLModel):
    """Response of ``POST /login/session/refresh``: a fresh access token.

    The rotated refresh token travels only in the ``cinna_refresh`` cookie.
    """

    access_token: str
    token_type: str = "bearer"
    # Access-token lifetime in seconds (``ACCESS_TOKEN_EXPIRE_MINUTES * 60``).
    expires_in: int
