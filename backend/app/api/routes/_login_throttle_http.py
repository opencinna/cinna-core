"""HTTP mapping of the login throttle (C4).

One 429 shape for every throttled login surface — the same body the MFA
limiter already uses, plus ``Retry-After`` (listed in the CORS
``expose_headers`` so the cross-origin SPA can read it).
"""
from fastapi import HTTPException, Request, status

from app.services.users.login_throttle_service import LoginThrottle, LoginThrottled

RATE_LIMITED_DETAIL = {"code": "rate_limited", "message": "rate_limited"}


def throttled_http(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=RATE_LIMITED_DETAIL,
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


def native_token_throttle(request: Request) -> None:
    """Route dependency for ``/desktop-auth/token`` and ``/app-auth/token``.

    Declared in ``dependencies=`` so it resolves before ``SessionDep``: a
    throttled request costs no pool connection.
    """
    try:
        LoginThrottle.check_native_token(request)
    except LoginThrottled as exc:
        raise throttled_http(exc.retry_after)
