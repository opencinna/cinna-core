"""Unit tests for ``GoogleChatAdapter``'s attachment media-fetch URL building.

Cross-reference: the end-to-end "an attachment is materialised into a
FileUpload" and "a DRIVE_FILE attachment is never fetched" scenarios are
covered over the full webhook pipeline in
``tests/api/server_channels/server_channels_attachments_test.py``, which
mocks ``fetch_attachment`` wholesale. This file is the pure, I/O-free half of
``fetch_attachment``'s own contract (plan §4.2): the URL it builds, that it is
run through the egress guard, and that a shape-invalid ``resourceName`` is
refused *before* any request is attempted — none of which needs a database, a
TestClient, or a real HTTP round trip, so it lives here rather than in
``tests/api/`` (see "Cross-reference convention" in ``tests/README.md``).

``GoogleChatAdapter._media_url`` is the right seam to test this at: it is the
staticmethod that does the shape validation and the egress-guard call, and it
returns *before* ``fetch_attachment`` ever opens an ``httpx.AsyncClient`` — so
calling it directly proves "refused before any request is made" by
construction rather than by asserting an HTTP mock was not awaited.
"""
from unittest.mock import patch

import pytest

from app.services.server_channels.adapters.base import ChannelAttachmentUnavailable
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

_EGRESS_GUARD_TARGET = "app.services.server_channels.adapters.google_chat.assert_url_allowed"


def _passthrough_guard():
    """A stand-in ``assert_url_allowed`` that allows everything and records
    what it was called with — used to prove *which* URL the media fetch would
    have used, without touching a real DNS lookup or making the test's
    egress-guard behavior a variable this file also has to control."""
    calls: list[str] = []

    def _guard(url: str) -> str:
        calls.append(url)
        return url

    return _guard, calls


def test_valid_resource_name_builds_a_url_under_the_chat_media_host_and_passes_the_guard() -> None:
    guard, calls = _passthrough_guard()
    with patch(_EGRESS_GUARD_TARGET, side_effect=guard):
        url = GoogleChatAdapter._media_url("AAAAneCC7B8/attachments/ACDIfx0123-abc_DEF")

    assert url.startswith("https://chat.googleapis.com/v1/media/")
    assert "AAAAneCC7B8/attachments/ACDIfx0123-abc_DEF" in url
    assert url.endswith("?alt=media")
    # The guard was actually consulted, and on the exact URL returned.
    assert calls == [url]


@pytest.mark.parametrize(
    "resource_name",
    [
        "../../etc/passwd",
        "AAAAneCC7B8/../../../secrets",
        "http://evil.example/steal",
        "https://chat.googleapis.com/v1/media/AAAA?alt=media",
        "AAAA/../BBBB",
    ],
)
def test_a_resource_name_with_dotdot_or_a_scheme_is_refused_before_any_request(
    resource_name: str,
) -> None:
    """Shape-invalid handles (a path-traversal segment, or a value that is
    already a URL rather than a bare token) must be refused by
    ``_media_url``'s own regex/``..`` check, never reaching
    ``assert_url_allowed`` at all — "refused before any request is made" is
    true by construction here: the guard is never even consulted, let alone
    an HTTP client opened."""
    guard, calls = _passthrough_guard()
    with patch(_EGRESS_GUARD_TARGET, side_effect=guard):
        with pytest.raises(ChannelAttachmentUnavailable) as exc_info:
            GoogleChatAdapter._media_url(resource_name)

    assert exc_info.value.reason == "invalid_handle"
    assert calls == [], (
        "the egress guard must never be consulted for a shape-invalid handle"
    )


def test_egress_guard_refusal_becomes_a_sender_safe_upstream_error() -> None:
    """When the *shape* is fine but the egress guard itself blocks the URL
    (a rebind, a disallowed host), the reason must still be a coarse,
    sender-safe token — never the guard's own exception text."""
    from app.services.common.egress_guard import EgressBlockedError

    with patch(_EGRESS_GUARD_TARGET, side_effect=EgressBlockedError("blocked for test")):
        with pytest.raises(ChannelAttachmentUnavailable) as exc_info:
            GoogleChatAdapter._media_url("AAAAneCC7B8/attachments/ACDIfx0123")

    assert exc_info.value.reason == "upstream_error"
