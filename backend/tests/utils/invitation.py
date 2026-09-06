"""HTTP helpers for the invitation surface, and the traps they exist to avoid.

Two things in here are not conveniences — they are the difference between a
test that proves something and one that passes for the wrong reason.

**THE EMAIL PATCH TARGET.** The test container runs mailcatcher, so a real
SMTP connection *succeeds silently*: a live 500 in the mail path shows up as a
green test. And ``patch("app.utils.send_email")`` does nothing at all to a
module that did ``from app.utils import send_email`` at import time — the name
the caller resolves is the module-local binding, not the one in ``app.utils``.
So every send in here is patched at the **binding site**:

* ``app.services.users.invitation_service.send_email`` — the invitation email;
* ``app.services.users.user_service.send_email`` — password recovery.

:func:`invitation_email_patched` and :func:`recovery_email_patched` also pin
``settings.emails_enabled`` rather than inheriting it. The compose override
sets ``SMTP_HOST: "mailcatcher"``, so email is **on** by default here — which
means a test asserting "nothing was sent because email is off" has to blank
the host itself, and one asserting a send has to not depend on the compose
file continuing to set one. Both directions go through :func:`_email_gate`.

A patch that silently misses is invisible, so every caller pairs these with an
``assert mock.call_count == …`` on a path whose sending behaviour is known —
including the zero cases, which is the only thing that can catch a target that
stopped matching.

**THE PROCESS-LOCAL RATE LIMITER.** ``RateLimiter._hits`` on
``app.api.routes.invitations._limiter`` is a module-global dict. It survives
transaction rollback, so two tests that each make a dozen lookups will trip the
limiter in whichever runs second and the failure reads like a logic bug.
:func:`fresh_invitation_limiter` swaps in a clean one per test.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR

INVITE_URL = f"{API}/users/invite"
LOOKUP_URL = f"{API}/invitations/lookup"
ACCEPT_URL = f"{API}/invitations/accept"

#: The one body every rejected token gets from ``POST /invitations/accept``.
INVITATION_INVALID_DETAIL = "This invitation is no longer valid"

#: The one body ``POST /password-recovery/{email}`` gives every caller.
RECOVERY_MESSAGE = (
    "If an account exists for that email, a password recovery email has been sent"
)


# ── Rate limiter ───────────────────────────────────────────────────────


def fresh_invitation_limiter(monkeypatch) -> None:
    """Give the calling test its own bucket for the two anonymous routes.

    Imported inside the function for the same reason
    ``tests/api/server_config/access_policy_test.py`` does it: the module-level
    limiter has no HTTP surface that can reset it, and this is the single named
    place the import lives.
    """
    from app.api.routes import invitations
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(invitations, "_limiter", RateLimiter())


# ── Email ──────────────────────────────────────────────────────────────


@contextmanager
def _email_gate(*, enabled: bool) -> Iterator[None]:
    """Pin ``settings.emails_enabled`` for the block, in either direction.

    ``emails_enabled`` is ``SMTP_HOST and EMAILS_FROM_EMAIL``, and **both are
    populated in the test container** — it points at mailcatcher, which is the
    same fact that makes an unpatched send succeed silently. So neither
    direction can be left to the environment:

    * ``enabled=True`` pins a fake host, so a test does not start depending on
      the compose file continuing to configure one;
    * ``enabled=False`` blanks ``SMTP_HOST``, because the container's default
      is *on*. Asserting "no mail was sent because email is off" without this
      asserts nothing — it was the shape of the first draft of the
      ``emails_enabled`` case here, and it failed honestly the moment it ran.
    """
    host = "smtp.example.com" if enabled else ""
    with (
        patch.object(settings, "SMTP_HOST", host),
        patch.object(settings, "SMTP_USER", "admin@example.com"),
        patch.object(settings, "EMAILS_FROM_EMAIL", "info@example.com"),
    ):
        yield


@contextmanager
def invitation_email_patched(
    *, side_effect: Exception | None = None, emails_enabled: bool = True
) -> Iterator[MagicMock]:
    """Intercept the invitation email at ``invitation_service``'s binding."""
    with ExitStack() as stack:
        stack.enter_context(_email_gate(enabled=emails_enabled))
        mock = stack.enter_context(
            patch(
                "app.services.users.invitation_service.send_email",
                MagicMock(side_effect=side_effect),
            )
        )
        yield mock


@contextmanager
def recovery_email_patched(
    *, side_effect: Exception | None = None, emails_enabled: bool = True
) -> Iterator[MagicMock]:
    """Intercept the password-recovery email at ``user_service``'s binding."""
    with ExitStack() as stack:
        stack.enter_context(_email_gate(enabled=emails_enabled))
        mock = stack.enter_context(
            patch(
                "app.services.users.user_service.send_email",
                MagicMock(side_effect=side_effect),
            )
        )
        yield mock


# ── Tokens ─────────────────────────────────────────────────────────────


def token_of(accept_url: str) -> str:
    """The ``?token=`` an accept URL carries."""
    token = parse_qs(urlparse(accept_url).query).get("token", [""])[0]
    assert token, f"No token in accept_url {accept_url!r}"
    return token


def claims_of(token: str) -> dict[str, Any]:
    """A token's claims, without verifying anything.

    Signature verification is off deliberately: the point is to read what the
    server minted (the ``jti``, the purpose) for tests that then re-mint or
    compare, not to re-implement the server's own verification.
    """
    return jwt.decode(token, options={"verify_signature": False})


def jti_of(token: str) -> str:
    return str(claims_of(token)["jti"])


def foreign_signed_token(email: str) -> str:
    """A well-formed invite-shaped JWT signed with the wrong key."""
    return jwt.encode(
        {
            "exp": 4102444800,  # 2100-01-01
            "sub": email,
            "jti": str(uuid.uuid4()),
            "purpose": "invite",
        },
        "not-the-servers-secret-key",
        algorithm="HS256",
    )


# ── Admin lifecycle routes ─────────────────────────────────────────────


def invite_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    email: str,
    role: str = "agent-user",
    full_name: str | None = None,
    auth_hint: str = "any",
    include_desktop: bool | None = None,
    managed_credential_ids: list[str] | None = None,
    send_email: bool = False,
    is_active: bool | None = True,
    expected_status: int = 200,
) -> dict[str, Any]:
    """``POST /users/invite``.

    ``send_email`` defaults to False. Email is *enabled* in the test container
    (it points at mailcatcher), so an unpatched invite with ``send_email=True``
    would open a real SMTP connection that succeeds silently — the exact shape
    that hides a live failure. Tests that are about the mail pass
    ``send_email=True`` inside :func:`invitation_email_patched`.

    ``is_active=None`` and ``full_name=None`` **omit the key from the body**
    rather than sending ``null``. That is the wire spelling of "the admin did
    not say", and on the adoption path it is the whole point: ``None`` on
    :class:`InviteUserRequest` means the submission was silent about that
    piece of persistent account state, so an adopted row keeps whatever it
    already held. The two spellings are equivalent by design — a test that
    pins the *schema's* acceptance of an explicit ``"is_active": null`` posts
    the body inline, because that one is about the bytes.
    """
    payload: dict[str, Any] = {
        "email": email,
        "role": role,
        "auth_hint": auth_hint,
        "send_email": send_email,
    }
    if is_active is not None:
        payload["is_active"] = is_active
    if full_name is not None:
        payload["full_name"] = full_name
    if include_desktop is not None:
        payload["include_desktop"] = include_desktop
    if managed_credential_ids is not None:
        payload["managed_credential_ids"] = managed_credential_ids

    response = client.post(
        INVITE_URL, headers=superuser_token_headers, json=payload
    )
    assert response.status_code == expected_status, (
        f"invite_user expected {expected_status}, got "
        f"{response.status_code}: {response.text}"
    )
    return response.json()


def invite_and_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    email: str,
    **kwargs: Any,
) -> tuple[dict[str, Any], str]:
    """Invite, and hand back ``(response_body, accept_token)``."""
    body = invite_user(
        client, superuser_token_headers, email=email, **kwargs
    )
    return body, token_of(body["accept_url"])


def get_invitation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    user_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{API}/users/{user_id}/invitation", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def resend_invitation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    user_id: str,
) -> httpx.Response:
    """Raw response — every caller here asserts on its status."""
    return client.post(
        f"{API}/users/{user_id}/invitation/resend",
        headers=superuser_token_headers,
    )


def revoke_invitation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    user_id: str,
) -> httpx.Response:
    return client.post(
        f"{API}/users/{user_id}/invitation/revoke",
        headers=superuser_token_headers,
    )


def get_invitation_link(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    user_id: str,
) -> httpx.Response:
    return client.get(
        f"{API}/users/{user_id}/invitation/link",
        headers=superuser_token_headers,
    )


# ── The two anonymous routes ───────────────────────────────────────────


def lookup(client: TestClient, token: str) -> httpx.Response:
    """``POST /invitations/lookup`` — raw, because the bytes are the contract."""
    return client.post(LOOKUP_URL, json={"token": token})


def accept(
    client: TestClient,
    token: str,
    password: str,
    *,
    full_name: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"token": token, "password": password}
    if full_name is not None:
        payload["full_name"] = full_name
    return client.post(ACCEPT_URL, json=payload)


# ── Assertions used by more than one file ──────────────────────────────


def assert_one_distinct_response(responses: list[httpx.Response], label: str) -> None:
    """Every response is byte-identical to every other one.

    Compared as ``(status_code, raw content)`` and **against each other**, not
    against a hardcoded literal: a copy-edit to the shared refusal string then
    cannot split the set into two groups that each still match their own
    expected constant.
    """
    seen = {(r.status_code, r.content) for r in responses}
    assert len(seen) == 1, (
        f"{label}: expected exactly one distinct (status, body); got "
        f"{sorted((s, c[:200]) for s, c in seen)}"
    )
