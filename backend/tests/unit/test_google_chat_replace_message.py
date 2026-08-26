"""`GoogleChatAdapter.replace_message` — delivering into an existing message.

This is the status notice's endgame: the message that has been saying "working
on your message…" is rewritten to hold the agent's answer, so the reader sees
one bot message per turn.

It exists because the obvious alternative does not work. Posting the reply and
then deleting the notice leaves Chat's **"Message deleted by its author"**
tombstone sitting above every single answer.

No HTTP here — the two primitives (`_patch_text`, `_post_chunks`) are patched,
so what is under test is the composition: which chunk goes where, what happens
when the patch fails, and that the markdown translation runs exactly once.

`ChannelReplaceResult.replaced` is asserted on every path because the caller
acts on it in opposite directions: a real replacement releases the thread's
status notice id, a fallback post keeps it. Reporting the fallback as a
replacement is what orphaned the "working on your message…" message — nothing
owned it afterwards and nothing could rewrite it again.

And the mirror-image defect, which survived a first fix round: reporting a
real replacement as a fallback. Once the patch has landed the message holds
the answer, so a *later* failure — the remaining chunks not posting — must not
take `replaced` back down to False. `_deliver` binds it before its `try`, so a
raise from the remainder post kept the notice id on the binding and the flush
loop patched the next spinner over a delivered reply. See
`test_a_failed_remainder_still_reports_the_slot_as_taken`.

The API-observable side is
`tests/api/server_channels/server_channels_status_notice_test.py`.
"""
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.server_channels.adapters.base import (
    ChannelReplaceResult,
    ChannelSendError,
)
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

_THREAD = "spaces/AAA/threads/BBB"
_NOTICE = "spaces/AAA/messages/notice-1"


def _adapter() -> GoogleChatAdapter:
    return GoogleChatAdapter()


# ---------------------------------------------------------------------------
# The message resource name
# ---------------------------------------------------------------------------


def test_message_url_accepts_a_chat_resource_name() -> None:
    assert GoogleChatAdapter._message_url(_NOTICE).endswith(f"/v1/{_NOTICE}")


@pytest.mark.parametrize(
    "bogus",
    ["", "fake-ext-id", "spaces/AAA", "spaces/AAA/threads/BBB", "messages/x"],
)
def test_message_url_refuses_anything_else(bogus: str) -> None:
    # An id that did not come from `send_message` cannot address a patch or a
    # delete. Refusing on the string beats appending it to the API base and
    # spending a round trip to discover a 404.
    with pytest.raises(ChannelSendError):
        GoogleChatAdapter._message_url(bogus)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_reply_that_fits_takes_the_slot_and_posts_nothing() -> None:
    adapter = _adapter()
    with patch.object(adapter, "_patch_text", AsyncMock()) as patch_text, patch.object(
        adapter, "_post_chunks", AsyncMock()
    ) as post_chunks:
        result = await adapter.replace_message(None, _THREAD, _NOTICE, "**done**")

    assert result == ChannelReplaceResult(message_id=_NOTICE, replaced=True)
    # Translated exactly once on the way in: `**done**` → `*done*`, not
    # `_done_`, which is what a second pass over Chat markup would produce.
    assert patch_text.await_args.args[2] == "*done*"
    post_chunks.assert_not_awaited()


@pytest.mark.anyio
async def test_an_oversized_reply_takes_the_slot_and_spills_after_it() -> None:
    adapter = _adapter()
    long_text = "\n".join(f"line {i}" for i in range(2000))
    with patch.object(adapter, "_patch_text", AsyncMock()) as patch_text, patch.object(
        adapter, "_post_chunks", AsyncMock(return_value="spaces/AAA/messages/last")
    ) as post_chunks:
        result = await adapter.replace_message(None, _THREAD, _NOTICE, long_text)

    chunks = adapter._chunk(long_text)
    assert len(chunks) > 1, "fixture must actually exceed the message limit"
    # First chunk into the slot, the rest posted after it — the difference from
    # `update_message`, which truncates because a patch addresses one message.
    assert patch_text.await_args.args[2] == chunks[0]
    assert post_chunks.await_args.args[2] == chunks[1:]
    assert result == ChannelReplaceResult(
        message_id="spaces/AAA/messages/last", replaced=True
    )


@pytest.mark.anyio
async def test_a_failed_patch_falls_back_to_posting_the_reply() -> None:
    """Losing the slot is cosmetic; losing the answer is not.

    The notice may have been deleted by hand, or its id may have gone stale.
    The fallback re-sends the **untranslated** text, because `send_message`
    translates and a second pass over Chat markup corrupts it.

    `replaced=False` is the load-bearing half of the answer: the named message
    is still standing and still says whatever it said, so the caller must KEEP
    the status notice id rather than release it.
    """
    adapter = _adapter()
    with patch.object(
        adapter, "_patch_text", AsyncMock(side_effect=ChannelSendError("gone"))
    ), patch.object(
        adapter, "send_message", AsyncMock(return_value="spaces/AAA/messages/new")
    ) as send:
        result = await adapter.replace_message(None, _THREAD, _NOTICE, "**done**")

    assert result == ChannelReplaceResult(
        message_id="spaces/AAA/messages/new", replaced=False
    )
    assert send.await_args.args[2] == "**done**"


@pytest.mark.anyio
async def test_no_slot_is_an_ordinary_send() -> None:
    adapter = _adapter()
    with patch.object(
        adapter, "send_message", AsyncMock(return_value="spaces/AAA/messages/new")
    ) as send:
        assert await adapter.replace_message(
            None, _THREAD, "", "hi"
        ) == ChannelReplaceResult(
            message_id="spaces/AAA/messages/new", replaced=False
        )
    assert send.await_args.args[2] == "hi"


# ---------------------------------------------------------------------------
# update_message — the OTHER rewrite verb, and how it differs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_message_truncates_rather_than_chunking() -> None:
    """The documented difference from `replace_message`, asserted nowhere else.

    A patch addresses exactly one message. `replace_message` chunks an
    oversized delivery and posts the remainder as new messages because it can
    — it is a delivery, and the caller gets the last id back. `update_message`
    has no second id to hand back, so oversized text is truncated to the
    limit instead of silently spilling into a message nothing points at.
    """
    adapter = _adapter()
    long_text = "**" + ("z" * 6000) + "**"  # translated -> one asterisk each side
    translated = f"*{'z' * 6000}*"
    assert len(translated) > 4096, "fixture must actually exceed the message limit"

    with patch.object(adapter, "_patch_text", AsyncMock()) as patch_text:
        await adapter.update_message(None, _THREAD, _NOTICE, long_text)

    patch_text.assert_awaited_once()
    sent_text = patch_text.await_args.args[2]
    assert sent_text == translated[:4096]
    assert len(sent_text) == 4096


@pytest.mark.anyio
async def test_update_message_with_no_slot_is_a_no_op() -> None:
    adapter = _adapter()
    with patch.object(adapter, "_patch_text", AsyncMock()) as patch_text:
        await adapter.update_message(None, _THREAD, "", "hi")
    patch_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# delete_message — zero coverage before this file
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Just enough of an ``httpx.Response`` for `_request_with_retries`."""

    def __init__(self, status_code: int = 200, *, bodyless: bool = False) -> None:
        self.status_code = status_code
        self._bodyless = bodyless

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("DELETE", "https://chat.googleapis.com/x")
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=request, response=self
            )

    def json(self) -> dict:
        if self._bodyless:
            raise ValueError("no body")
        return {}


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` — records calls, returns a fixed response."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def request(self, method, url, *, params=None, json=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json}
        )
        return self._response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patched_transport(adapter: GoogleChatAdapter, fake_client: _FakeAsyncClient):
    """Patch credential loading + the HTTP client so `delete_message` runs
    end to end without any real network or channel row."""
    return (
        patch.object(GoogleChatAdapter, "_load_credentials", staticmethod(lambda ch: {})),
        patch.object(
            adapter, "_mint_access_token", AsyncMock(return_value="tok")
        ),
        patch(
            "app.services.server_channels.adapters.google_chat.httpx.AsyncClient",
            lambda **kw: fake_client,
        ),
    )


@pytest.mark.anyio
async def test_delete_message_tolerates_a_404() -> None:
    """Already gone — by hand, or by this app on a retried tick — is success.

    `delete_message` sets `tolerate_missing=True`; without it a 404 would
    raise `ChannelSendError` and the caller would record a delivery failure
    on a binding whose notice is in exactly the state it wanted.
    """
    adapter = _adapter()
    fake_client = _FakeAsyncClient(_FakeResponse(status_code=404))
    p1, p2, p3 = _patched_transport(adapter, fake_client)
    with p1, p2, p3:
        result = await adapter.delete_message(None, _THREAD, _NOTICE)

    assert result is None
    assert fake_client.calls == [
        {
            "method": "DELETE",
            "url": GoogleChatAdapter._message_url(_NOTICE),
            "params": None,
            "json": None,
        }
    ]


@pytest.mark.parametrize(
    "bogus", ["", "fake-ext-id", "spaces/AAA", "spaces/AAA/threads/BBB"]
)
@pytest.mark.anyio
async def test_delete_message_refuses_a_non_resource_name(bogus: str) -> None:
    """The same resource-name guard `replace_message`/`update_message` share.

    An empty id is a silent no-op (nothing was ever posted to delete); a
    non-empty id that is not a Chat message resource name is refused loudly,
    via `_message_url`, before any HTTP call is attempted.
    """
    adapter = _adapter()
    if not bogus:
        # No HTTP client needed — the guard on `external_message_id` itself
        # returns before `_message_url` is even reached.
        assert await adapter.delete_message(None, _THREAD, bogus) is None
        return
    with pytest.raises(ChannelSendError):
        await adapter.delete_message(None, _THREAD, bogus)


@pytest.mark.anyio
async def test_request_with_retries_returns_empty_on_a_bodyless_delete() -> None:
    """The `except ValueError` branch: DELETE answers with an empty body.

    `response.json()` raises on an empty body, and that is not a failure —
    it is what a successful DELETE looks like. Without the branch, an
    otherwise-successful delete would raise out of `_request_with_retries`
    and be reported as a failed one.
    """
    adapter = _adapter()
    fake_client = _FakeAsyncClient(_FakeResponse(status_code=200, bodyless=True))

    result = await adapter._request_with_retries(
        client=fake_client,
        method="DELETE",
        url=GoogleChatAdapter._message_url(_NOTICE),
        params=None,
        payload=None,
        access_token="tok",
        channel=None,
    )

    assert result == {}


# ---------------------------------------------------------------------------
# The partial failure: patched, then the remainder could not be posted
# ---------------------------------------------------------------------------


@contextmanager
def _unswallowed_adapter_warnings():
    """Real ``LogRecord``s off the adapter's own logger.

    ``caplog`` cannot answer this once ``setup_db`` has run Alembic:
    ``alembic.config.Config`` calls ``logging.config.fileConfig`` with the
    default ``disable_existing_loggers=True``, which leaves every application
    logger permanently ``disabled`` for the rest of the session (see
    ``tests/README.md``, "caplog assertions are vacuous for the rest of the
    session"). So the handler is attached to the module logger directly and
    the logger force-enabled for the duration — the same manoeuvre as
    ``tests/unit/test_channel_outbound_instrumentation.py``.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(
        "app.services.server_channels.adapters.google_chat"
    )
    handler = _Collector(level=logging.WARNING)
    was_disabled, previous_level = target.disabled, target.level
    target.addHandler(handler)
    target.disabled = False
    target.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.disabled = was_disabled
        target.setLevel(previous_level)


@pytest.mark.anyio
async def test_a_failed_remainder_still_reports_the_slot_as_taken() -> None:
    """The patch landed, so `replaced=True` is no longer negotiable.

    The message NOW HOLDS chunk 0 of the answer. Ownership transferred at the
    patch, and losing it afterwards is the one outcome `ChannelReplaceResult`
    exists to prevent: `_deliver` binds `replaced = False` *before* its `try`,
    so a raise out of the remainder post reported the pre-patch value, kept
    the status notice id on the binding — and 45 seconds later the flush loop
    patched "working on your message…" straight over a delivered reply.

    The deliberate cost, documented on `replace_message`: a **truncated**
    reply is reported as delivered. Strictly better than a complete reply
    being patched away next turn, and the warning keeps it diagnosable —
    which is why the log line is asserted here and not left implicit.
    """
    adapter = _adapter()
    long_text = "\n".join(f"line {i}" for i in range(2000))
    chunks = adapter._chunk(long_text)
    assert len(chunks) > 1, "fixture must actually exceed the message limit"

    with patch.object(adapter, "_patch_text", AsyncMock()) as patch_text, patch.object(
        adapter,
        "_post_chunks",
        # The real shapes: a token that expired between the patch and the
        # post, a space that cannot be derived from the thread key, retries
        # exhausted against a Chat 5xx.
        AsyncMock(side_effect=ChannelSendError("token minting failed")),
    ) as post_chunks, _unswallowed_adapter_warnings() as records:
        result = await adapter.replace_message(None, _THREAD, _NOTICE, long_text)

    patch_text.assert_awaited_once()
    post_chunks.assert_awaited_once()
    # `replaced=True` is the load-bearing half: it is what makes `_deliver`
    # RELEASE the notice id, so nothing rewrites the message again.
    assert result == ChannelReplaceResult(message_id=_NOTICE, replaced=True)
    assert len(records) == 1
    assert "truncated" in records[0].getMessage()
    # The operator needs to know which message and how much is missing.
    assert _NOTICE in records[0].getMessage()
    assert str(len(chunks) - 1) in records[0].getMessage()


@pytest.mark.anyio
async def test_a_failed_remainder_is_not_swallowed_for_a_single_chunk() -> None:
    """The guard must not turn `_post_chunks` into an unconditional no-op.

    A one-chunk reply never reaches the remainder post at all, so this pins
    that the truncation branch above is genuinely the multi-chunk edge and
    not a blanket swallow the single-chunk path now also travels.
    """
    adapter = _adapter()
    with patch.object(adapter, "_patch_text", AsyncMock()), patch.object(
        adapter, "_post_chunks", AsyncMock(side_effect=ChannelSendError("boom"))
    ) as post_chunks, _unswallowed_adapter_warnings() as records:
        result = await adapter.replace_message(None, _THREAD, _NOTICE, "short")

    post_chunks.assert_not_awaited()
    assert result == ChannelReplaceResult(message_id=_NOTICE, replaced=True)
    assert records == []
