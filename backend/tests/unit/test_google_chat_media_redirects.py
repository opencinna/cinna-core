"""Unit tests for ``GoogleChatAdapter._download_media`` / ``_resolve_redirect``
— the Google Chat attachment media fetch's redirect-chain handling.

Chat media does not hand back bytes directly: it answers with a redirect to a
signed URL on a *different* Google origin, so the fetch must follow
redirects — but the request carries this app's ``chat.bot`` bearer token, so
what happens to that token across a redirect is a real credential-leak
surface. See ``_resolve_redirect``'s own docstring in
``app/services/server_channels/adapters/google_chat.py`` for the full
reasoning; this file proves the three properties it states rather than
trusting the docstring on faith — the same posture
``test_google_chat_replace_message.py`` and
``server_channels_security_invariants_test.py``'s JWT tests already take in
this domain.

Uses ``httpx.MockTransport`` against the adapter's real ``_download_media``
so the actual request/response loop runs, not a re-description of it — the
egress guard (``assert_url_allowed``) is patched to a pass-through spy since
its own rebind defence does a real blocking DNS lookup that has no place in
a unit test, but everything about *which* URL gets which headers is real.

Pure, I/O-free (against a real network) and DB-free, so this lives in
``tests/unit/`` rather than in the API-level attachments test file — see the
"Cross-reference convention" in ``tests/README.md``.
"""
import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.services.server_channels.adapters.base import ChannelAttachmentUnavailable
from app.services.server_channels.adapters.google_chat import (
    _MEDIA_MAX_REDIRECTS,
    GoogleChatAdapter,
)

_EGRESS_GUARD_TARGET = "app.services.server_channels.adapters.google_chat.assert_url_allowed"

_ORIGINAL_URL = "https://chat.googleapis.com/v1/media/AAAA?alt=media"


def _allow_all(url: str) -> str:
    return url


def _spying_guard():
    calls: list[str] = []

    def _guard(url: str) -> str:
        calls.append(url)
        return url

    return _guard, calls


def _patched_client(transport: httpx.MockTransport):
    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return _PatchedClient


def _run_download(adapter: GoogleChatAdapter, *, url: str = _ORIGINAL_URL, ceiling: int = 10_000_000):
    return asyncio.run(
        adapter._download_media(url=url, access_token="secret-bot-token", ceiling=ceiling, timeout=5.0)
    )


# ---------------------------------------------------------------------------
# The Authorization latch: A -> B (cross-origin) -> A-again
# ---------------------------------------------------------------------------


def test_authorization_is_stripped_cross_origin_and_never_resurrected_on_return_to_the_original_host() -> None:
    """
    Chain: chat.googleapis.com (A) --redirect--> storage.googleapis.com (B,
    cross-origin: the bearer token must be dropped here) --redirect-->
    chat.googleapis.com again (A-again).

    The THIRD hop's target is textually back on the original Chat host, and a
    reader could be tempted to "simplify" the stripping logic into a per-hop
    check — "is this hop's target the same origin as the *original* URL?" —
    which would resurrect the token here. The real implementation instead
    latches once the chain has left the origin
    (``send_authorization = send_authorization and keep_authorization``) and
    never re-arms, so the third hop must NOT carry the token either, even
    though it looks like a return to safety.
    """
    hop2_url = "https://storage.googleapis.com/bucket/obj?sig=xyz"
    hop3_url = "https://chat.googleapis.com/v1/media/AAAA-final?alt=media"

    responses = [
        httpx.Response(302, headers={"location": hop2_url}),
        httpx.Response(302, headers={"location": hop3_url}),
        httpx.Response(200, content=b"the real attachment bytes"),
    ]
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return responses[len(seen_requests) - 1]

    transport = httpx.MockTransport(handler)

    with patch("httpx.AsyncClient", _patched_client(transport)), patch(
        _EGRESS_GUARD_TARGET, side_effect=_allow_all
    ):
        result = _run_download(GoogleChatAdapter())

    assert result == b"the real attachment bytes"
    assert len(seen_requests) == 3

    def _auth(req: httpx.Request) -> str | None:
        return req.headers.get("authorization")

    assert _auth(seen_requests[0]) == "Bearer secret-bot-token", (
        "the first hop, still on the Chat origin, must carry the bot token"
    )
    assert _auth(seen_requests[1]) is None, (
        "the cross-origin hop must not carry the bot token"
    )
    assert _auth(seen_requests[2]) is None, (
        "the token must not be resurrected on the third hop just because its "
        "target is textually back on chat.googleapis.com — the latch must "
        "not re-arm once the chain has left the origin"
    )


# ---------------------------------------------------------------------------
# http:// downgrade — refused regardless of host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "downgrade_location",
    [
        pytest.param(
            "http://chat.googleapis.com/v1/media/other?alt=media", id="same-host-downgrade"
        ),
        pytest.param("http://attacker.example/steal", id="cross-host-downgrade"),
    ],
)
def test_http_downgrade_redirect_is_refused_before_the_egress_guard_is_even_consulted(
    downgrade_location: str,
) -> None:
    """``egress_guard.ALLOWED_SCHEMES`` includes ``"http"`` (it has other
    callers), so the egress guard does NOT catch a scheme downgrade — the
    adapter's own scheme check in ``_resolve_redirect`` is the only thing
    that does, and it must fire before the target is even worth checking:
    same host or a different one, a downgrade always sends the request (and
    on a same-host hop, the still-attached bearer token) over cleartext."""
    responses = [httpx.Response(302, headers={"location": downgrade_location})]
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return responses[len(seen_requests) - 1]

    transport = httpx.MockTransport(handler)
    guard, guard_calls = _spying_guard()

    with patch("httpx.AsyncClient", _patched_client(transport)), patch(
        _EGRESS_GUARD_TARGET, side_effect=guard
    ):
        with pytest.raises(ChannelAttachmentUnavailable) as exc_info:
            _run_download(GoogleChatAdapter())

    assert exc_info.value.reason == "upstream_error"
    assert guard_calls == [], (
        "the scheme check must refuse the downgrade before ever calling "
        "assert_url_allowed on the http:// target"
    )
    assert len(seen_requests) == 1, "no further hop should have been attempted"


# ---------------------------------------------------------------------------
# Redirect cap — a clean ChannelAttachmentUnavailable, never a raw exception
# ---------------------------------------------------------------------------


def test_a_redirect_chain_exceeding_the_cap_raises_a_clean_channel_error() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        next_url = f"https://chat.googleapis.com/v1/media/hop-{call_count}?alt=media"
        return httpx.Response(302, headers={"location": next_url})

    transport = httpx.MockTransport(handler)

    with patch("httpx.AsyncClient", _patched_client(transport)), patch(
        _EGRESS_GUARD_TARGET, side_effect=_allow_all
    ):
        with pytest.raises(ChannelAttachmentUnavailable) as exc_info:
            _run_download(GoogleChatAdapter())

    assert exc_info.value.reason == "upstream_error"
    # The loop makes exactly _MEDIA_MAX_REDIRECTS + 1 attempts before giving up.
    assert call_count == _MEDIA_MAX_REDIRECTS + 1, call_count
