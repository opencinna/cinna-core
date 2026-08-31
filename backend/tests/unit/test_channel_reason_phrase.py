"""Unit tests for ``channel_inbound_service._reason_phrase`` — the one place
that renders a channel-attachment skip *code* into sender-facing prose.

Cross-reference: the sender-visible surfaces this feeds (the transcript note,
the ``REPLY_ATTACHMENTS_REJECTED`` decline) are exercised end to end in
``tests/api/server_channels/server_channels_attachments_test.py``. This file
pins the pure mapping itself — no I/O, no DB, no HTTP — which is exactly the
kind of private pure-logic helper ``tests/README.md``'s "Unit Tests" section
says belongs here rather than in an API test.

Why this exists: ``_reason_phrase``'s docstring states the property in as
many words — "an unrecognised code falls back to a generic sentence, never to
the code itself" — because this text reaches an *external, unauthenticated*
sender's own Chat thread. A raw token like ``drive_file`` or
``poll_budget_exhausted`` leaking into that sentence is an internal
identifier leaking outward; a test that renders every code the adapters can
actually emit and asserts none of the resulting sentences contains an
underscore is what would have caught it — the docstring says this exact
mistake was made and caught by hand during implementation, not by a test,
until now.

The code list is **derived from the function's own source**, not
hand-copied, specifically so the next code added to ``_reason_phrase``'s
``phrases`` dict is picked up automatically instead of silently under-tested
the way a hardcoded 14-entry list already was once: the codebase added a
15th (``fetch_budget_exhausted``, split out of ``timeout`` for a fetch task
cancelled while still queued rather than in flight) between one draft of
this file and the next, and a fixed literal would have kept passing while
covering only 14 of the 15. ``test_the_derivation_is_not_vacuous`` is the
self-check that keeps this derivation itself honest — the shape mirrors
"the failing message in the middle of the tick" pattern used elsewhere in
this change: prove the checker can fail, not just that it currently passes.
"""
import inspect
import re

from app.services.server_channels.channel_inbound_service import (
    _UNKNOWN_REASON_PHRASE,
    _reason_phrase,
)

# The known-minimum count at the time this derivation was written (plan
# review found 14, then a 15th — fetch_budget_exhausted — landed before this
# file did). Asserted as a floor, not an exact count: the whole point of
# deriving the set from source is that a future addition should not require
# editing this number, but a floor still catches the set going EMPTY, which
# would mean the derivation itself broke.
_MINIMUM_KNOWN_EMITTABLE_CODES = 15


def _emittable_reason_codes() -> list[str]:
    """The keys of ``_reason_phrase``'s own ``phrases = {...}`` dict literal,
    read out of its source rather than copied by hand.

    Bounded to the dict-literal body (from ``"phrases = {"` to its closing
    brace) before matching quoted keys, so a docstring or comment elsewhere
    in the function that happens to quote a lowercase/underscore word can
    never be picked up as a code — the regex only ever looks inside the one
    block that IS the contract.
    """
    source = inspect.getsource(_reason_phrase)
    start = source.index("phrases = {")
    body = source[start:]
    close_at = body.index("\n    }\n")
    body = body[:close_at]
    return re.findall(r'^\s*"([a-z_]+)":', body, re.MULTILINE)


def test_the_derivation_is_not_vacuous() -> None:
    """A guard that can't fail isn't a guard: prove the source-derived list
    actually reflects real dict keys, not an empty or bogus match."""
    codes = _emittable_reason_codes()
    assert len(codes) >= _MINIMUM_KNOWN_EMITTABLE_CODES, codes
    assert len(codes) == len(set(codes)), f"duplicate key extracted: {codes}"
    # Every extracted code must be a REAL key of the dict — i.e. must not
    # fall back to the generic phrase — which is the same property the tests
    # below assert for the whole set, checked here as the derivation's own
    # sanity check rather than assumed.
    assert all(_reason_phrase(code) != _UNKNOWN_REASON_PHRASE for code in codes)
    # And the extraction must be able to tell a real code apart from a
    # fabricated one — if this ever started matching everything (a
    # regression in the bounding logic above), an obviously-fake code would
    # incorrectly appear to be "known".
    assert "not_a_real_reason_code_xyz" not in codes


def test_every_emittable_reason_code_renders_to_prose_with_no_underscore_token() -> None:
    for code in _emittable_reason_codes():
        phrase = _reason_phrase(code)
        assert "_" not in phrase, (
            f"_reason_phrase({code!r}) == {phrase!r} contains a raw "
            "underscore-token fragment — an internal skip code is leaking "
            "into a sender-visible sentence on an unauthenticated ingress."
        )
        assert phrase, f"_reason_phrase({code!r}) must not be empty"


def test_every_emittable_reason_code_has_its_own_dedicated_phrase() -> None:
    """None of the documented codes may be silently falling back to the
    generic unknown-reason sentence — that would mean the mapping has
    drifted out of sync with what the adapters/materialiser actually emit."""
    for code in _emittable_reason_codes():
        assert _reason_phrase(code) != _UNKNOWN_REASON_PHRASE, (
            f"{code!r} is falling back to the generic phrase — the mapping "
            "in _reason_phrase no longer covers a code the feature emits."
        )


def test_an_unrecognised_code_falls_back_to_the_generic_phrase_not_the_raw_token() -> None:
    """The safety property the whole module exists for: an adapter that
    invents a new code with no matching prose entry must degrade to a vague
    sentence, never to echoing the code verbatim."""
    phrase = _reason_phrase("some_future_code_nobody_mapped_yet")
    assert phrase == _UNKNOWN_REASON_PHRASE
    assert "_" not in phrase
    assert "some_future_code_nobody_mapped_yet" not in phrase
