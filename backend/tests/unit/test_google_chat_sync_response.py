"""GoogleChatAdapter.build_sync_response — the webhook's own HTTP reply.

Pure: builds a dict, no I/O. Two facts, and the first one is a bug fix rather
than a refinement.

A Chat app's synchronous response with no ``thread`` is posted as a **new
top-level message in the space**, not into the conversation that triggered it.
Every other message the pipeline sends goes through ``send_message``, which
names ``thread.name`` explicitly — so the sync reply was the one message in the
exchange that landed somewhere else. The sender saw "finding an assistant…" in
the room and every later word inside a thread.

The second fact is the same translation ``send_message`` does: the reply text
is authored in markdown like everything else on this path.
"""
from app.services.server_channels.adapters.google_chat import GoogleChatAdapter

_THREAD = "spaces/AAA/threads/BBB"


def test_sync_response_is_addressed_to_the_triggering_thread() -> None:
    body = GoogleChatAdapter().build_sync_response("Denied.", _THREAD)
    assert body == {"text": "Denied.", "thread": {"name": _THREAD}}


def test_sync_response_without_a_thread_stays_a_space_level_post() -> None:
    # The `added_to_space` welcome: there is no thread yet, and a space-level
    # greeting is the correct shape for it.
    body = GoogleChatAdapter().build_sync_response("Hi!")
    assert body == {"text": "Hi!"}
    assert "thread" not in body


def test_sync_response_translates_markdown_like_every_other_outbound_text() -> None:
    body = GoogleChatAdapter().build_sync_response("Setting up **X**", _THREAD)
    assert body["text"] == "Setting up *X*"


def test_no_text_is_a_silent_acknowledgement() -> None:
    assert GoogleChatAdapter().build_sync_response(None, _THREAD) == {}
    assert GoogleChatAdapter().build_sync_response("", _THREAD) == {}
