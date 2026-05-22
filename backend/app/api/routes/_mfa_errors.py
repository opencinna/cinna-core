"""Shared HTTP error translation for the 2FA / MFA flows.

``MfaService`` raises ``ValueError(code)`` on every failure path; the
route layer (``routes/login.py``, ``routes/mfa.py``, ``routes/oauth.py``)
maps those codes to HTTP status codes and a stable ``error.detail.code``
payload the frontend can branch on.

Kept in a single module so the mapping cannot drift between callers.
"""
from __future__ import annotations

from fastapi import HTTPException


_STATUS_BY_CODE: dict[str, int] = {
    "invalid_code": 400,
    "invalid_assertion": 400,
    "invalid_secret_token": 400,
    "invalid_method": 400,
    "challenge_not_found": 404,
    "passkey_not_found": 404,
    "factor_not_enrolled": 404,
    "challenge_expired": 410,
    "challenge_consumed": 410,
    "attempt_limit_exceeded": 429,
    "rate_limited": 429,
    "totp_already_enrolled": 409,
    "step_up_required": 401,
}


def translate_mfa_error(exc: ValueError) -> HTTPException:
    """Map a ``ValueError(code)`` from :class:`MfaService` to an
    :class:`HTTPException` with the conventional status and a stable
    ``error.detail.code`` payload."""
    code = str(exc)
    status = _STATUS_BY_CODE.get(code, 400)
    return HTTPException(
        status_code=status, detail={"code": code, "message": code}
    )
