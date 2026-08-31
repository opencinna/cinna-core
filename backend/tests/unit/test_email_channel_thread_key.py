"""Unit tests for the email transport's composite thread key.

``build_reply_thread_key`` / ``parse_reply_thread_key``
(``app/services/server_channels/adapters/email.py``) are pure string logic
with no I/O — a textbook tests/unit/ candidate per backend/tests/README.md,
same posture as ``GoogleChatAdapter._chunk``
(tests/unit/test_google_chat_adapter_chunk.py).

The API-observable side — an actual inbound/outbound round trip producing an
``OutgoingEmailQueue`` row with these exact headers — is covered in
tests/api/server_channels/server_channels_email_test.py
(``test_reply_carries_in_reply_to_and_references_headers``); see that file's
module docstring for the cross-reference back to this one.

Plan §6 calls out the separator specifically: the join character is ``|``, but
the split marker used by the parser is the three-character sequence ``>|<``,
because a bare Message-ID may legally contain ``|`` (it is RFC 5322 ``atext``)
and splitting on the character alone would be ambiguous. Both halves are
angle-bracketed before they are ever joined, so ``>`` and ``<`` — which cannot
appear *inside* a Message-ID, only as its delimiters — make the split
unambiguous. The tests below exercise exactly that: a Message-ID containing a
literal ``|``, where a naive ``str.split("|")`` would produce the wrong
answer.
"""
from app.services.server_channels.adapters.email import (
    build_reply_thread_key,
    parse_reply_thread_key,
)

_ROOT = "<root-abc@test.example>"
_LAST = "<last-xyz@test.example>"


def test_build_then_parse_round_trips_root_and_last() -> None:
    composite = build_reply_thread_key(_ROOT, _LAST)
    assert composite == f"{_ROOT}|{_LAST}"

    root, last = parse_reply_thread_key(composite)
    assert root == _ROOT
    assert last == _LAST


def test_build_with_no_last_message_id_yields_the_bare_root() -> None:
    composite = build_reply_thread_key(_ROOT, None)
    assert composite == _ROOT

    root, last = parse_reply_thread_key(composite)
    assert root == _ROOT
    assert last is None


def test_build_collapses_when_last_equals_root() -> None:
    """The first reply in a thread: nothing to distinguish, so no separator."""
    composite = build_reply_thread_key(_ROOT, _ROOT)
    assert composite == _ROOT
    assert parse_reply_thread_key(composite) == (_ROOT, None)


def test_round_trip_survives_a_message_id_containing_a_pipe_character() -> None:
    """The headline trap: ``|`` is legal RFC 5322 atext inside a Message-ID.

    A naive ``composite.split("|")`` would cut the root itself in half here.
    The parser must instead split on the ``>|<`` sequence — the point where
    one id's closing bracket meets the next id's opening bracket — which
    cannot occur inside either id.
    """
    root_with_pipe = "<ro|ot-abc@test.example>"
    last_with_pipe = "<la|st-xyz@test.example>"

    composite = build_reply_thread_key(root_with_pipe, last_with_pipe)
    assert composite == f"{root_with_pipe}|{last_with_pipe}"

    root, last = parse_reply_thread_key(composite)
    assert root == root_with_pipe
    assert last == last_with_pipe


def test_parse_a_bare_root_with_no_composite_marker_yields_no_last_id() -> None:
    root, last = parse_reply_thread_key(_ROOT)
    assert root == _ROOT
    assert last is None


def test_parse_degrades_to_the_bare_key_when_the_split_marker_is_absent() -> None:
    """An unrecognisable key still yields a usable root rather than raising.

    The worst outcome of a malformed thread key is a reply sent without
    threading headers — refusing to send would lose the answer entirely, and
    the binding lookup (keyed on the *root*) must still succeed.
    """
    weird = "not-a-message-id-at-all"
    root, last = parse_reply_thread_key(weird)
    assert root == weird
    assert last is None
