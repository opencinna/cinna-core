"""HTTP side of browser cookie sessions: cookie, Origin check, login issuance.

The service (``WebSessionService``) knows nothing about HTTP; everything
cookie- or header-shaped lives here so every web login issuer goes through
one function, :func:`issue_login_token`.

* Opt-in: a login creates a cookie session only with ``X-Cinna-Web-Session: 1``
  (the SPA always sends it). Scripts and tests get a bearer-only token, no
  cookie and no row.
* Cookie ``cinna_refresh``: HttpOnly, SameSite=Strict, host-only, scoped to
  ``/api/v1/login/session`` so it reaches nothing but the refresh/logout
  endpoints. ``Secure`` per :func:`refresh_cookie_secure`.
* CSRF: the session-bound token from ``LoginToken.csrf_token`` comes back as
  ``X-CSRF-Token``; on top of that a present ``Origin`` must be an allowed
  CORS origin. The latter is mandatory, not decorative: ``CORSMiddleware``
  reflects any origin with credentials.
"""
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.core.config import settings
from app.models import LoginToken, User
from app.services.users.auth_service import AuthService
from app.services.users.web_session_service import (
    ERROR_CSRF_FAILED,
    ERROR_SESSION_EXPIRED,
    WebSessionService,
)

REFRESH_COOKIE_NAME = "cinna_refresh"
REFRESH_COOKIE_PATH = f"{settings.API_V1_STR}/login/session"
WEB_SESSION_HEADER = "X-Cinna-Web-Session"
CSRF_HEADER = "X-CSRF-Token"


def refresh_cookie_secure() -> bool:
    """``WEB_SESSION_COOKIE_SECURE``, defaulting to "not the local stack"."""
    if settings.WEB_SESSION_COOKIE_SECURE is not None:
        return settings.WEB_SESSION_COOKIE_SECURE
    return settings.ENVIRONMENT != "local"


def set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw,
        max_age=settings.WEB_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=refresh_cookie_secure(),
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=refresh_cookie_secure(),
    )


def csrf_failed_http() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_CSRF_FAILED)


def session_expired_response() -> JSONResponse:
    """401 ``session_expired`` that also clears the refresh cookie.

    Returned rather than raised: an ``HTTPException`` would drop the
    ``Set-Cookie`` that clears the cookie.
    """
    response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": ERROR_SESSION_EXPIRED},
    )
    clear_refresh_cookie(response)
    return response


def require_allowed_origin(request: Request) -> None:
    """403 ``csrf_failed`` when a present ``Origin`` is not an allowed origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    if origin.rstrip("/") not in settings.all_cors_origins:
        raise csrf_failed_http()


def web_session_opt_in(
    x_cinna_web_session: Annotated[
        str | None, Header(alias=WEB_SESSION_HEADER)
    ] = None,
) -> bool:
    """Whether this login asked for a cookie session (``X-Cinna-Web-Session: 1``)."""
    return (x_cinna_web_session or "").strip() == "1"


WebSessionOptIn = Annotated[bool, Depends(web_session_opt_in)]


def issue_login_token(
    session: Session,
    user: User,
    request: Request,
    response: Response,
    *,
    opt_in: bool,
    trusted_device_token: str | None = None,
) -> LoginToken:
    """The one way a web login hands out its access token.

    With ``opt_in``: opens a web session, sets the refresh cookie on
    ``response``, mints the access token with ``sid`` and returns the CSRF
    token once. Without: a bearer-only token, no cookie, no row.
    """
    if not opt_in:
        return LoginToken(
            access_token=AuthService.create_user_access_token(user),
            trusted_device_token=trusted_device_token,
        )
    web_session, refresh_raw, csrf_raw = WebSessionService.create(
        session, user, request.headers.get("user-agent")
    )
    set_refresh_cookie(response, refresh_raw)
    return LoginToken(
        access_token=AuthService.create_user_access_token(
            user, web_session_id=web_session.id
        ),
        trusted_device_token=trusted_device_token,
        csrf_token=csrf_raw,
    )
