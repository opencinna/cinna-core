"""Helpers for Server Channels API tests.

Covers three things:

1. Plain HTTP wrappers around the admin CRUD + auto-install-list routes
   (``POST /admin/server-channels``, etc.) — ordinary API helpers, no
   exemptions needed.
2. A self-contained Google Chat JWT/JWKS test double (``GoogleChatJWTSigner``)
   used to exercise the *real* ``verify_google_signed_jwt`` verification path
   (including its ``(JoseError, ValueError)`` handling) without talking to
   Google. Only the JWKS *fetch* is mocked (``app.core.security.
   _get_google_certs``); the JWT itself is genuinely RS256-signed and
   genuinely verified.
3. ``flush_pending_bindings`` — a documented Rule-1 exemption, mirroring
   ``tests/utils/session.py``'s "Active-streaming-manager helpers" and
   ``tests/utils/platform_token.py``: the pending-flush scheduler entry point
   has no HTTP surface at all (the scheduler itself is TESTING-gated — see
   ``app/services/server_channels/channel_pending_scheduler.py`` — and tests
   are explicitly meant to call the underlying service method directly, per
   the feature plan's own testing checklist). This is the single, named place
   that import lives so individual test files stay free of ``app.services``
   imports.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlmodel import Session

from app.core.config import settings
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_ADMIN_BASE = f"{API}/admin/server-channels"

CHAT_ISSUER = "chat@system.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Admin channel CRUD
# ---------------------------------------------------------------------------


def create_server_channel(
    client: TestClient,
    token_headers: dict[str, str],
    *,
    channel_type: str = "google_chat",
    name: str | None = None,
    enabled: bool = True,
    auto_register_users: bool = False,
    project_number: str = "123456789012",
    email_whitelist: str | None = None,
    secrets: str | None = None,
    expected_status: int = 200,
) -> dict:
    """Create a server channel via POST /admin/server-channels."""
    payload: dict[str, Any] = {
        "channel_type": channel_type,
        "name": name or f"channel-{random_lower_string()[:8]}",
        "enabled": enabled,
        "auto_register_users": auto_register_users,
        "config": {"project_number": project_number},
    }
    if email_whitelist is not None:
        payload["email_whitelist"] = email_whitelist
    if secrets is not None:
        payload["secrets"] = secrets
    r = client.post(_ADMIN_BASE, headers=token_headers, json=payload)
    assert r.status_code == expected_status, (
        f"Create server channel failed: {r.status_code} {r.text}"
    )
    return r.json()


def list_server_channels(client: TestClient, token_headers: dict[str, str]) -> list[dict]:
    r = client.get(_ADMIN_BASE, headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def get_setup_instructions(
    client: TestClient, token_headers: dict[str, str], channel_id: str
) -> dict:
    r = client.get(f"{_ADMIN_BASE}/{channel_id}/setup-instructions", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def update_server_channel(
    client: TestClient,
    token_headers: dict[str, str],
    channel_id: str,
    *,
    expected_status: int = 200,
    **fields: Any,
) -> dict:
    r = client.put(f"{_ADMIN_BASE}/{channel_id}", headers=token_headers, json=fields)
    assert r.status_code == expected_status, (
        f"Update server channel failed: {r.status_code} {r.text}"
    )
    return r.json()


def delete_server_channel(
    client: TestClient, token_headers: dict[str, str], channel_id: str
) -> None:
    r = client.delete(f"{_ADMIN_BASE}/{channel_id}", headers=token_headers)
    assert r.status_code == 204, r.text


def send_test_outbound(
    client: TestClient,
    token_headers: dict[str, str],
    channel_id: str,
    *,
    thread_key: str | None = "spaces/AAA",
    email: str | None = None,
    text: str | None = None,
    expect_status: int = 200,
) -> dict:
    """Fire the admin test-send.

    Exactly one of ``thread_key`` / ``email`` reaches the route — pass
    ``thread_key=None`` to target by email. ``expect_status`` exists for the
    "both targets" 422 case, which never reaches the body assertion.
    """
    payload: dict[str, Any] = {}
    if thread_key is not None:
        payload["thread_key"] = thread_key
    if email is not None:
        payload["email"] = email
    if text is not None:
        payload["text"] = text
    r = client.post(
        f"{_ADMIN_BASE}/{channel_id}/test-outbound", headers=token_headers, json=payload
    )
    assert r.status_code == expect_status, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Debug panel
# ---------------------------------------------------------------------------


def list_debug_events(
    client: TestClient, token_headers: dict[str, str], channel_id: str
) -> dict:
    r = client.get(
        f"{_ADMIN_BASE}/{channel_id}/debug-events", headers=token_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def clear_debug_events(
    client: TestClient, token_headers: dict[str, str], channel_id: str
) -> None:
    r = client.delete(
        f"{_ADMIN_BASE}/{channel_id}/debug-events", headers=token_headers
    )
    assert r.status_code == 200, r.text


def list_recent_senders(
    client: TestClient, token_headers: dict[str, str], channel_id: str
) -> list[dict]:
    r = client.get(
        f"{_ADMIN_BASE}/{channel_id}/recent-senders", headers=token_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Auto-install list
# ---------------------------------------------------------------------------


def list_auto_install_bundles(client: TestClient, token_headers: dict[str, str]) -> list[dict]:
    r = client.get(f"{_ADMIN_BASE}/auto-install-list", headers=token_headers)
    assert r.status_code == 200, r.text
    return r.json()


def add_auto_install_bundle(
    client: TestClient,
    token_headers: dict[str, str],
    bundle_uuid: str,
    *,
    expected_status: int = 200,
) -> Any:
    r = client.post(
        f"{_ADMIN_BASE}/auto-install-list",
        headers=token_headers,
        json={"bundle_uuid": bundle_uuid},
    )
    assert r.status_code == expected_status, (
        f"Add auto-install bundle failed: {r.status_code} {r.text}"
    )
    return r.json() if r.status_code == 200 else None


def remove_auto_install_bundle(
    client: TestClient, token_headers: dict[str, str], bundle_uuid: str
) -> None:
    r = client.delete(f"{_ADMIN_BASE}/auto-install-list/{bundle_uuid}", headers=token_headers)
    assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# Google Chat webhook event payloads
# ---------------------------------------------------------------------------


def build_message_event(
    *,
    thread_key: str,
    text: str,
    sender_email: str | None = "sender@example.com",
    sender_name: str | None = None,
    sender_display_name: str = "Test Sender",
    sender_type: str = "HUMAN",
    message_name: str | None = None,
) -> dict:
    """A Google Chat ``MESSAGE`` interaction event."""
    return {
        "type": "MESSAGE",
        "message": {
            "name": message_name or f"spaces/AAA/messages/{uuid.uuid4()}",
            "sender": {
                "name": sender_name or f"users/{uuid.uuid4().hex[:12]}",
                "displayName": sender_display_name,
                "email": sender_email,
                "type": sender_type,
            },
            "text": text,
            "argumentText": text,
            "thread": {"name": thread_key},
        },
    }


def build_added_to_space_event() -> dict:
    return {"type": "ADDED_TO_SPACE"}


def build_ignored_event() -> dict:
    """An authentic event the pipeline should never act on (a bot's own post)."""
    return {
        "type": "MESSAGE",
        "message": {
            "name": f"spaces/AAA/messages/{uuid.uuid4()}",
            "sender": {"name": "users/bot", "type": "BOT"},
            "text": "I am the bot echoing myself",
            "thread": {"name": "spaces/AAA/threads/ignored"},
        },
    }


def post_webhook(
    client: TestClient,
    webhook_token: str,
    event: dict,
    *,
    bearer_token: str | None = "unused",
    headers: dict[str, str] | None = None,
):
    """POST an event to the public webhook.

    ``bearer_token=None`` omits the Authorization header entirely (the
    "no header" probe case). Pass an explicit ``headers`` dict to control the
    Authorization header precisely (e.g. a garbage non-JWT string).
    """
    req_headers = dict(headers or {})
    if bearer_token is not None and "Authorization" not in req_headers:
        req_headers["Authorization"] = f"Bearer {bearer_token}"
    return client.post(
        f"{API}/channels/{webhook_token}/inbound", json=event, headers=req_headers
    )


# ---------------------------------------------------------------------------
# Google Chat JWT / JWKS test double
# ---------------------------------------------------------------------------


class GoogleChatJWTSigner:
    """A throwaway RSA keypair + matching JWKS, for signing test webhook JWTs.

    Exercises the real ``verify_google_signed_jwt`` verification path end to
    end (RS256 signature check, issuer/audience claims, and — the point of
    this whole helper — Authlib's bare ``ValueError`` on an unrecognized
    ``kid``) without any network call. Patch ``app.core.security.
    _get_google_certs`` with :meth:`patched` so the adapter's JWKS fetch
    resolves to this signer's public JWKS instead of hitting Google.
    """

    def __init__(self, kid: str = "test-kid-1") -> None:
        self.kid = kid
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        jwk = json.loads(RSAAlgorithm.to_jwk(self._private_key.public_key()))
        jwk.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        self.jwks: dict[str, Any] = {"keys": [jwk]}

    def token(
        self,
        *,
        audience: str,
        issuer: str = CHAT_ISSUER,
        kid: str | None = None,
        extra_claims: dict[str, Any] | None = None,
        expired: bool = False,
        kid_override: str | bool = False,
    ) -> str:
        """Mint a signed webhook bearer token.

        ``kid`` selects which key-set entry the header claims to use;
        defaults to this signer's own (verifiable) kid. Pass an unrelated
        string (e.g. ``"unknown-kid-xyz"``) to produce the "unknown kid"
        probe — Authlib cannot find it in the JWKS and raises a bare
        ``ValueError`` deep in its key-set lookup, which is exactly the bug
        this helper exists to regression-test.
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "iat": now - 5,
            "exp": (now - 5) if expired else (now + 3600),
        }
        if extra_claims:
            claims.update(extra_claims)
        headers = {"kid": self.kid if kid is None else kid}
        return pyjwt.encode(
            claims, self._private_pem, algorithm="RS256", headers=headers
        )

    @contextmanager
    def patched(self):
        """Patch the JWKS fetch so verification resolves against this signer."""
        with patch(
            "app.core.security._get_google_certs",
            AsyncMock(return_value=self.jwks),
        ):
            yield


# ---------------------------------------------------------------------------
# Pending-flush scheduler entry point — Rule-1 exemption
# ---------------------------------------------------------------------------


def flush_pending_bindings(db: Session) -> int:
    """Directly invoke ``ChannelInboundService.flush_pending_bindings``.

    EXEMPTION — no HTTP surface exists for this. It is the scheduler's own
    entry point (``app/services/server_channels/channel_pending_scheduler.py``),
    and the scheduler is gated behind ``settings.TESTING`` per project
    convention specifically so tests call this directly instead of racing a
    background thread — the feature plan's own testing checklist says so
    explicitly. Mirrors the exemption pattern in
    ``tests/utils/session.py`` (active-streaming-manager helpers) and
    ``tests/utils/platform_token.py``: the one import lives here, named,
    so individual test files stay free of ``app.services`` imports.

    Any ``agent_env_connector`` stub the caller needs active for a resulting
    ingest (draining parked messages can create a session and kick off
    streaming) must be patched by the caller *around* this call, exactly as
    for any other session-driving helper.
    """
    from app.services.server_channels.channel_inbound_service import (
        ChannelInboundService,
    )

    return asyncio.run(ChannelInboundService.flush_pending_bindings(db))


# ---------------------------------------------------------------------------
# Pass 1 ownership filter — Rule-1 exemption
# ---------------------------------------------------------------------------


def route_installed(db: Session, user, text: str):
    """Directly invoke ``ChannelRoutingService._route_installed``.

    EXEMPTION — same shape as ``flush_pending_bindings`` above: this is a
    private routing-filter step with no HTTP surface of its own (it only
    runs inside ``ChannelRoutingService.decide``, reachable otherwise only via a full
    webhook round trip with a real Pass-1 route set up per case). It is also
    the load-bearing security boundary documented on the method itself
    ("channel sessions must run on the sender's own install" — the same
    invariant ``ChannelIngestionService.assert_access`` asserts for
    ``channel_caller``), so it earns a direct, cheap test rather than only
    incidental coverage through the full-flow routing tests.

    ``user`` must be a real, persisted ``app.models.User`` row (fetched via
    ``db.get(User, ...)`` after creating the account through the API) —
    ``_route_installed`` reads ``user.id`` for the ownership comparison.
    """
    from app.services.server_channels.channel_routing_service import (
        ChannelRoutingService,
    )

    return ChannelRoutingService._route_installed(db, user, text)


def build_routing_result(
    *,
    agent_id: uuid.UUID,
    is_identity: bool = False,
    agent_name: str = "Mocked Route Result",
    session_mode: str = "conversation",
    route_source: str = "user",
    match_method: str = "only_one",
):
    """Build a ``RoutingResult`` for mocking ``AppMCPRoutingService.route_message``.

    EXEMPTION — ``RoutingResult`` is a plain dataclass in
    ``app.services.app_mcp.app_mcp_routing_service`` with no constructor
    exposed via any API. Hand-building one is the intended, cheap way to
    pin ``_route_installed``'s ownership-filter branches (identity route /
    foreign-owned agent / deleted agent / router exception) without paying
    for a full personal-route setup
    (``tests.utils.app_agent_route.create_user_route``) per case.
    """
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult

    return RoutingResult(
        agent_id=agent_id,
        agent_name=agent_name,
        session_mode=session_mode,
        route_id=uuid.uuid4(),
        route_source=route_source,
        match_method=match_method,
        is_identity=is_identity,
    )


__all__ = [
    "create_server_channel",
    "list_server_channels",
    "get_setup_instructions",
    "update_server_channel",
    "delete_server_channel",
    "send_test_outbound",
    "list_auto_install_bundles",
    "add_auto_install_bundle",
    "remove_auto_install_bundle",
    "build_message_event",
    "build_added_to_space_event",
    "build_ignored_event",
    "post_webhook",
    "GoogleChatJWTSigner",
    "flush_pending_bindings",
    "route_installed",
    "build_routing_result",
    "CHAT_ISSUER",
]
