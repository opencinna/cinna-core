"""Unit tests for ``find_seal_boundary`` (chat_text_chunking.py).

Pure text-offset logic, no I/O, no DB — a textbook tests/unit/ candidate per
backend/tests/README.md.

This file pins the freshly-extracted, previously-untested boundary-selection
logic described in ``docs/plans/google_chat_streaming_updates_plan.md`` §4
Phase 1. It is intentionally separate from
``tests/unit/test_google_chat_adapter_chunk.py``, which is a frozen behavior
pin for ``chunk_text``/``fence_open_after`` and must not be edited here or
elsewhere as part of this extraction.

Every case below was verified against the actual implementation (not just
against the docstring) before being written down, and exact offsets are
computed values, not estimates.
"""
import random

from app.services.server_channels.adapters.chat_text_chunking import find_seal_boundary
from app.services.server_channels.adapters.google_chat_format import (
    FENCE_RE,
    _take_fenced_block,
)


# ---------------------------------------------------------------------------
# `None` cases — "no acceptable boundary yet", not a failure
# ---------------------------------------------------------------------------


def test_window_not_positive_returns_none() -> None:
    text = "aaaa\nbbbb"
    assert find_seal_boundary(text, 0) is None
    assert find_seal_boundary(text, -1) is None


def test_text_fitting_in_window_returns_none() -> None:
    # len(text) <= window: nothing to seal off yet.
    assert find_seal_boundary("short", 100) is None
    assert find_seal_boundary("short", len("short")) is None


def test_boundary_at_exactly_index_window_is_rejected() -> None:
    """Pinned deliberately: a newline at index == window is NOT "inside" it.

    ``line_end < window`` is a strict inequality in the implementation. This
    guards against someone later "fixing" it into ``<=`` and sealing content
    that runs exactly up to (and including) the window boundary.
    """
    text = "aaaa\nbbbb"
    assert find_seal_boundary(text, 4) is None
    assert find_seal_boundary(text, 5) == 5


def test_no_newline_inside_window_returns_none() -> None:
    # One unbroken paragraph fills the whole window - no candidate newline.
    text = "x" * 50 + "\n" + "y" * 50
    assert find_seal_boundary(text, 10) is None


def test_open_fence_filling_the_window_returns_none() -> None:
    """A fence that never closes inside the window hides every newline in it.

    Several newlines lie inside the window, and each would otherwise be a
    perfectly good candidate (well past the floor) — but every one of them is
    inside the still-open fence, so none qualifies and the result is None,
    not the fence-blind boundary a naive newline scan would pick.
    """
    lines = [f"line{i:03d} of body text padding" for i in range(20)]
    text = "```\n" + "\n".join(lines) + "\n```\nafter the fence closes, with tail text"
    assert find_seal_boundary(text, 60) is None


def test_table_filling_the_window_does_not_return_none() -> None:
    """Contrast with the fence case above: a table's row ends ARE line breaks.

    With nothing above the table to back out to, the in-table split is simply
    taken (see the table-at-offset-0 test below for the same shape spelled
    out explicitly) — the window being "full of table" must not be confused
    with the window being "full of open fence".
    """
    row1, row2, row3 = "| a | b |", "| c | d |", "| e | f |"
    tail = "TAIL AFTER TABLE PADDING PADDING PADDING PADDING PADDING"
    text = f"{row1}\n{row2}\n{row3}\n{tail}"
    assert find_seal_boundary(text, 25) is not None


def test_every_candidate_below_floor_returns_none() -> None:
    """"Sure! Here's the answer.\\n\\n" + long body — the docstring's own example.

    The only paragraph break sits in the first ~19 characters, far below the
    floor for any reasonably-sized window; nothing else in the fixture is a
    candidate at all (single unbroken run of "x"), so the result is None
    regardless of the (generous) window chosen.
    """
    text = "Sure!\n\n" + "x" * 400
    assert find_seal_boundary(text, 200) is None
    assert find_seal_boundary(text, 50) is None


def test_preferred_candidate_with_empty_remainder_returns_none() -> None:
    # Only newlines follow the sole candidate boundary -> nothing to continue.
    text = "A" * 20 + "\n\n\n"
    assert find_seal_boundary(text, 22) is None


def test_earlier_candidate_is_not_retried_when_the_preferred_one_fails() -> None:
    """A perfectly valid EARLIER boundary is not used as a fallback.

    Line 1 ("A"*20) is a plain line-break candidate whose remainder ("B"*20 +
    "\\n\\n") is non-empty — sealing there would succeed. But line 2's
    boundary is a *paragraph* break (blank line follows, then end of text),
    so it wins the tier preference; its remainder is empty (only newlines
    follow), and the implementation does not fall back to line 1 — it simply
    returns None. This is what "earlier candidates are NOT retried" means:
    there is only ever one candidate remembered per tier (the last one seen),
    so there is nothing to retry from even if there wanted to be.
    """
    text = "A" * 20 + "\n" + "B" * 20 + "\n\n"
    assert len(text) > 42
    assert find_seal_boundary(text, 42) is None


# ---------------------------------------------------------------------------
# Paragraph vs. line-break preference
# ---------------------------------------------------------------------------


def test_paragraph_break_preferred_over_a_later_plain_newline() -> None:
    """A paragraph break beats a LATER plain line break, not just an earlier one.

    Layout: "A"*20 <line> "B"*20 <blank> "C"*20 <line> "D"*20 <line> tail.
    The paragraph break after "B"*20 is earlier in the text than the plain
    line break after "C"*20, yet it is still preferred — tier beats position.
    """
    first = "A" * 20
    para_line = "B" * 20
    tail_line = "C" * 20
    text = (
        first + "\n" + para_line + "\n\n" + tail_line + "\n" + "D" * 20
        + "\nEND_OF_DRAFT_TAIL_TEXT"
    )

    offset = find_seal_boundary(text, 80)

    assert offset == 43
    assert text[:offset].rstrip("\n") == first + "\n" + para_line
    assert text[offset:] == tail_line + "\nDDDDDDDDDDDDDDDDDDDD\nEND_OF_DRAFT_TAIL_TEXT"


def test_floor_applies_within_each_tier_independently() -> None:
    """A paragraph break below the floor does not win, and is not promoted.

    "Sure! Here is it." + blank line sits at the very start (well below the
    floor for a window covering the list that follows); a later plain line
    break within the list of items is what actually gets used. Pinned as its
    own test distinct from the "every candidate is below the floor" None
    case above: here there IS an acceptable candidate, just not the
    paragraph one.
    """
    items = "\n".join(
        f"- item number {i:03d} with a bit of extra padding text" for i in range(20)
    )
    text = "Sure! Here is it.\n\n" + items

    offset = find_seal_boundary(text, 200)

    assert offset is not None
    # Must NOT have sealed at the tiny paragraph break right after the intro.
    assert offset != 19
    sealed = text[:offset].rstrip("\n")
    assert sealed != "Sure! Here is it."
    assert 100 <= len(sealed) < 200  # floor = 200 // 2 = 100, and < window


# ---------------------------------------------------------------------------
# Fence avoidance
# ---------------------------------------------------------------------------


def test_never_cuts_inside_an_open_fence_but_accepts_the_closing_fence_line() -> None:
    """Opening-fence line rejected; lines strictly inside the fence rejected;
    the CLOSING fence line itself is a valid boundary (state has already
    toggled back to "closed" by the time that line's newline is considered).
    """
    head = "A" * 20
    body = "B" * 20
    tail = "C" * 40
    text = f"{head}\n```\n{body}\n```\n{tail}\nTAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER"

    offset = find_seal_boundary(text, 80)

    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    # Sealed content runs through the closing fence line, not before it and
    # not into the fenced body.
    assert sealed == f"{head}\n```\n{body}\n```"
    assert text[offset:].startswith("C" * 40)
    # Neither the opening fence line nor a line inside the fence was chosen:
    # if either had been, the sealed slice would not include the full body.
    assert body in sealed
    assert "```" in sealed


# ---------------------------------------------------------------------------
# Raw-markdown fence model regression pins
#
# ``find_seal_boundary`` used to detect fences with the TRANSLATED-space model
# (``line.lstrip().startswith("```")``) — blind to ``~~~`` and blind to marker
# identity. These pin the two inputs that first exposed the bug, plus the
# open-marker-matching semantics (mirroring ``google_chat_format``'s
# ``FENCE_RE`` / ``_take_fenced_block``) that replaced it.
# ---------------------------------------------------------------------------


def test_tilde_fence_regression_no_boundary_inside_the_block() -> None:
    """Was 73 (a cut through the middle of the ``~~~`` body) before the fix.

    73 is the newline after "line one of code" — a plain backtick-blind scan
    sees no fence at all in a ``~~~`` block and treats every line inside it as
    an ordinary candidate. The fixed scan tracks the open marker and rejects
    every line until the block closes, and no closing line falls inside this
    window, so the whole draft has no acceptable boundary yet.
    """
    text = (
        "Here is the script you asked for.\n\n"
        "~~~\n" "line one of code\n" "line two of code\n"
        "line three of code\n" "line four of code\n" "~~~\n"
        "That was the script.\n" + "tail padding\n" * 5
    )

    assert find_seal_boundary(text, 90) is None


def test_tilde_fence_regression_seals_right_after_the_closing_fence() -> None:
    """Pinned deliberately: proves the update-state-THEN-check ordering.

    114 is the newline ending the closing ``~~~`` line. The scan updates
    ``open_marker`` back to ``None`` for THIS line before deciding whether
    its own newline is a candidate, so the closing fence line itself is a
    legal boundary — only the OPENING fence line and the lines strictly
    between the two would have been refused.
    """
    text = (
        "Here is the script you asked for.\n\n"
        "~~~\n" "line one of code\n" "line two of code\n"
        "line three of code\n" "line four of code\n" "~~~\n"
        "That was the script.\n" + "tail padding\n" * 5
    )

    offset = find_seal_boundary(text, 130)

    assert offset == 114
    assert text[:offset].rstrip("\n").endswith("~~~")
    assert text[offset:].startswith("That was the script.")


def test_backtick_fence_regression_non_closing_marker_line_keeps_block_open() -> None:
    """Was 120 (a false boundary) before the fix.

    ``"``` not really a close"`` fails ``FENCE_RE`` (its info string contains
    a space), so it is ordinary content, not a close — the backtick block
    opened two lines above it is still open when the window ends, and the
    real closing ``` ``` ``` never falls inside this window, so there is no
    acceptable boundary yet.
    """
    text = (
        "Intro paragraph that is reasonably long here.\n"
        "```\n" "first code line\n" "``` not really a close\n"
        "more code here\n" "still more code\n" "```\n"
        "after\n" + "pad\n" * 8
    )

    assert find_seal_boundary(text, 120) is None


def test_backtick_fence_regression_seals_before_the_block_opens() -> None:
    """Was 87 (mid-block) before the fix; 46 seals the intro paragraph instead.

    46 is the newline ending the intro paragraph — the last candidate before
    the fence opens. With the block correctly recognised as open for the rest
    of the window, nothing inside it qualifies, so the intro paragraph break
    is the best (only) candidate.
    """
    text = (
        "Intro paragraph that is reasonably long here.\n"
        "```\n" "first code line\n" "``` not really a close\n"
        "more code here\n" "still more code\n" "```\n"
        "after\n" + "pad\n" * 8
    )

    offset = find_seal_boundary(text, 90)

    assert offset == 46
    assert text[:offset].rstrip("\n") == "Intro paragraph that is reasonably long here."
    assert text[offset:].startswith("```\n")


def test_mismatched_backtick_line_inside_a_tilde_block_does_not_close_it() -> None:
    """A ``` ``` ``` line inside a ``~~~`` block is content, not a close.

    If the scan mistakenly toggled on ANY fence marker (rather than requiring
    the SAME marker character), the ``` ``` ``` line here would be read as
    closing the ``~~~`` block, exposing "body line one" as a plain candidate
    at window=80 (it clears the floor). The correct model keeps the block
    open under the tilde marker straight through it, so window=80 has no
    boundary at all, and window=100 only reaches the REAL (tilde) close.
    """
    head = "A" * 20
    text = (
        f"{head}\n"
        "~~~\n"
        "```\n"
        "body line one padding text\n"
        "body line two padding text\n"
        "~~~\n"
        + "C" * 40 + "\n"
        "TAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER"
    )

    assert find_seal_boundary(text, 80) is None

    offset = find_seal_boundary(text, 100)
    assert offset == 87
    assert text[:offset].rstrip("\n") == (
        f"{head}\n~~~\n```\nbody line one padding text\nbody line two padding text\n~~~"
    )
    assert text[offset:].startswith("C" * 40)


def test_mismatched_tilde_line_inside_a_backtick_block_does_not_close_it() -> None:
    """Mirror of the case above with the marker roles swapped.

    A ``~~~`` line inside a ``` ``` ``` block closes nothing either — the
    model is symmetric in which marker character opened the block.
    """
    head = "A" * 20
    text = (
        f"{head}\n"
        "```\n"
        "~~~\n"
        "body line one padding text\n"
        "body line two padding text\n"
        "```\n"
        + "C" * 40 + "\n"
        "TAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER"
    )

    assert find_seal_boundary(text, 80) is None

    offset = find_seal_boundary(text, 100)
    assert offset == 87
    assert text[:offset].rstrip("\n") == (
        f"{head}\n```\n~~~\nbody line one padding text\nbody line two padding text\n```"
    )
    assert text[offset:].startswith("C" * 40)


def test_indented_fence_lines_are_recognized_as_fences() -> None:
    """A fence marker preceded by leading whitespace is still a fence.

    Without the leading-whitespace group in ``FENCE_RE``, an indented fence
    (common when a code block sits inside a list item) would read as content,
    and the block it opens would never be protected.
    """
    head = "A" * 20
    tail = "TAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER_PADDING"
    text = (
        f"{head}\n"
        "   ```py\n"
        "code line one here padding\n"
        "code line two here padding\n"
        "   ```\n"
        + "C" * 40 + "\n" + tail
    )

    assert find_seal_boundary(text, 60) is None  # window ends mid-(indented)-block

    offset = find_seal_boundary(text, 100)
    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    assert sealed.endswith("   ```")
    assert "code line two here padding" in sealed
    assert text[offset:].startswith("C" * 40)


def test_longer_marker_runs_and_info_strings_are_recognized_as_fences() -> None:
    """A 5-backtick marker with an info string is still a fence, same as ```."""
    head = "A" * 20
    tail = "TAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER_PADDING"
    text = (
        f"{head}\n"
        "`````python\n"
        "code line one here padding\n"
        "code line two here padding\n"
        "`````\n"
        + "C" * 40 + "\n" + tail
    )

    assert find_seal_boundary(text, 60) is None  # window ends mid-block

    offset = find_seal_boundary(text, 100)
    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    assert sealed.endswith("`````")
    assert "code line two here padding" in sealed
    assert text[offset:].startswith("C" * 40)


def test_lines_fence_re_rejects_are_ordinary_content_not_fence_markers() -> None:
    """Near-fence lines that fail ``FENCE_RE`` never toggle fence state.

    ``"``` trailing words"`` has a space INSIDE what would need to be a
    whitespace-free info string; ``"```a`b"`` has a backtick past the marker
    run; ``"``"`` is only two backticks (the marker run needs 3+). None of
    these open (or close) anything, so every newline after them is an
    ordinary, available candidate.
    """
    head = "A" * 20
    tail = "TAIL_MORE_TEXT_HERE_TO_PAD_OUT_FURTHER_PADDING"
    text = (
        f"{head}\n"
        "``` trailing words\n"
        "```a`b\n"
        "``\n"
        + "C" * 40 + "\n" + tail
    )

    offset = find_seal_boundary(text, 60)

    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    assert sealed == f"{head}\n``` trailing words\n```a`b\n``"
    assert text[offset:].startswith("C" * 40)


# ---------------------------------------------------------------------------
# Table walk-back
# ---------------------------------------------------------------------------


def test_table_split_is_walked_back_when_enough_content_precedes_it() -> None:
    """The general case: plenty of real content above the table -> the cut
    moves to just above the table's first row instead of splitting it.
    """
    prose = "PROSE_CONTENT_LINE_ABOVE_THE_TABLE_THAT_IS_LONG_ENOUGH"
    row1, row2, row3 = "| a | b |", "| c | d |", "| e | f |"
    tail = "TAIL AFTER TABLE, PADDING PADDING PADDING PADDING"
    text = f"{prose}\n{row1}\n{row2}\n{row3}\n{tail}"

    offset = find_seal_boundary(text, 80)

    assert offset is not None
    assert text[:offset].rstrip("\n") == prose
    # The WHOLE table survives intact in the remainder.
    assert text[offset:] == f"{row1}\n{row2}\n{row3}\n{tail}"


def test_table_split_accepted_when_table_starts_at_offset_zero() -> None:
    """Nothing precedes the table at all -> there is nothing to back out to,
    so the in-table split is taken as-is.
    """
    row1, row2, row3 = "| a | b |", "| c | d |", "| e | f |"
    tail = "TAIL AFTER TABLE PADDING PADDING PADDING PADDING PADDING"
    text = f"{row1}\n{row2}\n{row3}\n{tail}"

    offset = find_seal_boundary(text, 25)

    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    assert sealed == f"{row1}\n{row2}"
    assert text[offset:] == f"{row3}\n{tail}"


def test_table_split_accepted_when_walk_back_would_fall_below_the_floor() -> None:
    """Some real (non-blank) content precedes the table, but too little of it
    to clear the floor if walked back to -> the in-table split is kept.
    """
    prose = "Hi"
    row1, row2, row3 = "| a | b |", "| c | d |", "| e | f |"
    tail = "TAIL AFTER TABLE PADDING PADDING PADDING PADDING PADDING"
    text = f"{prose}\n{row1}\n{row2}\n{row3}\n{tail}"

    offset = find_seal_boundary(text, 26)

    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    assert sealed == f"{prose}\n{row1}\n{row2}"
    assert text[offset:] == f"{row3}\n{tail}"


def test_table_split_accepted_when_everything_above_the_table_is_blank() -> None:
    """The third, plan-list-missing acceptance path.

    The region above the table is long enough (in raw characters) to clear
    the floor's length check, but it is PURE WHITESPACE — no visible content
    at all. The walk-back's own length check alone would accept walking back
    to it, but the general "sealed content must contain non-whitespace"
    guarantee then rejects that walked-back boundary, so the function falls
    back to the original (un-walked-back, in-table) split rather than
    returning None.
    """
    prefix = " " * 40 + "\n"  # 40 blank chars: clears a floor of 30, but is whitespace-only
    row1, row2, row3 = "| a | b |", "| c | d |", "| e | f |"
    tail = "TAIL CONTENT AFTER TABLE PADDING TO MAKE REMAINDER NONEMPTY"
    text = prefix + row1 + "\n" + row2 + "\n" + row3 + "\n" + tail

    offset = find_seal_boundary(text, 60)

    assert offset is not None
    sealed = text[:offset].rstrip("\n")
    # The un-walked-back in-table split was taken: sealed content includes
    # the blank prefix and the first table row only.
    assert sealed == prefix.rstrip("\n") + "\n" + row1
    assert text[offset:] == f"{row2}\n{row3}\n{tail}"
    assert sealed.strip()  # contains non-whitespace (row1) even though the
    # walked-back candidate that was rejected would not have.


def test_table_separated_by_blank_line_is_not_treated_as_split_at_all() -> None:
    """A blank line between two pipe-row blocks ends the table there — the two
    blocks are two separate tables, and cutting between them is the ideal
    boundary (a paragraph break), not something to walk back from.
    """
    row1 = "| a | b |"
    row2 = "| c | d |"
    text = f"{row1}\n\n{row2}\nTAIL PADDING PADDING PADDING PADDING PADDING PADDING"

    offset = find_seal_boundary(text, 15)

    assert offset is not None
    assert text[:offset].rstrip("\n") == row1
    assert text[offset:].startswith(row2)


# ---------------------------------------------------------------------------
# Returned-offset guarantees, asserted directly
# ---------------------------------------------------------------------------


def test_returned_offset_guarantees_on_a_representative_case() -> None:
    first = "A" * 20
    para_line = "B" * 20
    tail_line = "C" * 20
    text = (
        first + "\n" + para_line + "\n\n" + tail_line + "\n" + "D" * 20
        + "\nEND_OF_DRAFT_TAIL_TEXT"
    )
    window = 80

    offset = find_seal_boundary(text, window)

    assert offset is not None
    assert 0 < offset <= len(text)
    assert text[offset - 1] == "\n"
    assert text[offset:] != ""
    assert not text[offset:].startswith("\n")
    assert text[:offset].strip() != ""
    sealed = text[:offset].rstrip("\n")
    assert window // 2 <= len(sealed) < window


def test_returned_offset_consumes_the_newline_run_but_not_a_whitespace_line() -> None:
    """Only newline characters are consumed by the boundary — a whitespace-only
    line (e.g. a line of trailing spaces) right after the boundary survives
    into the remainder rather than being swallowed as part of the "blank
    line" run.
    """
    head = "A" * 20
    # Paragraph break after `head`, then a whitespace-only line, then real
    # tail content so the remainder is non-empty.
    text = head + "\n\n" + "   " + "\n" + "TAIL" * 10

    offset = find_seal_boundary(text, 30)

    assert offset is not None
    assert text[:offset].rstrip("\n") == head
    # The whitespace-only line was NOT consumed as part of the newline run.
    assert text[offset:].startswith("   \n")


# ---------------------------------------------------------------------------
# Property test: documented int-return guarantees across many generated inputs
# ---------------------------------------------------------------------------


def _generate_case(rng: random.Random) -> tuple[str, int]:
    """A short, deterministic random markdown-ish fragment plus a window.

    Mixes plain prose lines, blank lines, code-fence markers, and pipe-table
    rows so both the fence and table code paths get exercised alongside
    plain paragraph/line breaks.
    """
    pieces: list[str] = []
    for _ in range(rng.randint(1, 25)):
        kind = rng.random()
        if kind < 0.08:
            pieces.append("```")
        elif kind < 0.15:
            pieces.append("")  # blank line
        elif kind < 0.30:
            cols = rng.randint(1, 3)
            row = "|" + "|".join(
                f" {rng.choice(['a', 'bb', 'ccc'])} " for _ in range(cols)
            ) + "|"
            pieces.append(row)
        else:
            length = rng.randint(1, 30)
            pieces.append("".join(rng.choice("abcdefg ") for _ in range(length)))
    text = "\n".join(pieces)
    window = rng.randint(1, max(2, len(text) + 5))
    return text, window


def test_int_return_guarantees_hold_over_many_generated_inputs() -> None:
    """Fast, deterministic property check — not a fuzz run (~3000 cases,
    well under a second) — over every documented guarantee for a non-None
    result: offset bounds, the trailing-newline landing spot, a non-empty
    and non-newline-leading remainder, non-whitespace sealed content, and
    the floor/window bracket on the sealed (rstripped) length.
    """
    rng = random.Random(20260831)
    checked_int_results = 0

    for _ in range(3000):
        text, window = _generate_case(rng)
        offset = find_seal_boundary(text, window)
        if offset is None:
            continue
        checked_int_results += 1

        assert 0 < offset <= len(text), (text, window, offset)
        assert text[offset - 1] == "\n", (text, window, offset)
        assert text[offset:] != "", (text, window, offset)
        assert not text[offset:].startswith("\n"), (text, window, offset)
        assert text[:offset].strip() != "", (text, window, offset)

        sealed = text[:offset].rstrip("\n")
        assert window // 2 <= len(sealed) < window, (text, window, offset, len(sealed))

    # Sanity: the generator actually produces sealable cases often enough
    # that this test is exercising the guarantees, not vacuously passing.
    assert checked_int_results > 500


# ---------------------------------------------------------------------------
# Renderer-agreement invariant: the real correctness criterion for the fix
# ---------------------------------------------------------------------------


def _renderer_open_marker_at_end(text: str) -> str | None:
    """The fence marker ``markdown_to_chat`` would still consider open after
    scanning all of ``text``, or ``None`` if every fenced block in it closes.

    Deliberately built ON TOP OF the renderer's own ``FENCE_RE`` and
    ``_take_fenced_block`` — not a hand-rolled fence parser — so this check
    cannot drift from what ``google_chat_format`` actually does. It cannot
    reuse ``_take_fenced_block``'s return value alone to tell "closed" from
    "ran off the end still open" (both consume every remaining line and
    return ``len(lines)``), so it additionally re-checks whether the last
    line the block actually consumed is itself a same-marker closing fence —
    guarded against the degenerate case where that "last consumed line" is
    the opening fence line itself (an unclosed one-line-fence at the very end
    of the text).
    """
    lines = text.split("\n")
    masked: list[str] = []
    out: list[str] = []
    index = 0
    while index < len(lines):
        fence = FENCE_RE.match(lines[index])
        if fence is None:
            index += 1
            continue
        start_index = index
        new_index = _take_fenced_block(lines, index, fence, masked, out)
        closed = False
        if new_index - 1 != start_index:
            closing = FENCE_RE.match(lines[new_index - 1])
            if closing is not None and closing.group(2)[0] == fence.group(2)[0]:
                closed = True
        if not closed:
            return fence.group(2)[0]
        index = new_index
    return None


def _generate_renderer_case(rng: random.Random) -> tuple[str, int]:
    """A short, deterministic fragment covering both fence markers, mismatched
    markers, fence-like lines ``FENCE_RE`` rejects, indented/long-run fences,
    and pipe tables — everything the fence-model fix and its regression pins
    above touch, generated together so the invariant test below sees
    combinations no single hand-written case spells out.
    """
    pieces: list[str] = []
    for _ in range(rng.randint(1, 20)):
        kind = rng.random()
        if kind < 0.12:
            indent = rng.choice(["", "  ", "    "])
            marker_char = rng.choice(["`", "~"])
            marker_len = rng.choice([3, 3, 3, 4, 5])
            info = rng.choice(["", "python", "txt"])
            sep = " " if rng.random() < 0.5 else ""
            pieces.append(f"{indent}{marker_char * marker_len}{sep}{info}")
        elif kind < 0.18:
            # Near-fence content that FENCE_RE must reject.
            pieces.append(rng.choice([
                "``` trailing words",
                "```a`b",
                "``",
                "~~ two tildes",
            ]))
        elif kind < 0.24:
            pieces.append("")  # blank line
        elif kind < 0.38:
            cols = rng.randint(1, 3)
            row = "|" + "|".join(
                f" {rng.choice(['a', 'bb', 'ccc'])} " for _ in range(cols)
            ) + "|"
            pieces.append(row)
        else:
            length = rng.randint(1, 30)
            pieces.append("".join(rng.choice("abcdefg ") for _ in range(length)))
    text = "\n".join(pieces)
    window = rng.randint(1, max(2, len(text) + 5))
    return text, window


def test_seal_boundary_never_lands_inside_a_block_the_renderer_treats_as_open() -> None:
    """The real correctness criterion: every sealed slice, handed to
    ``markdown_to_chat``'s own fence scan on its own, closes every fenced
    block it opens. This is a fast regression sentinel (a few hundred
    generated cases, well under a second) for a fix that was validated with
    an exhaustive 60k-case sweep offline — not a re-proof of that sweep.
    """
    rng = random.Random(20260831)
    checked_int_results = 0

    for _ in range(400):
        text, window = _generate_renderer_case(rng)
        offset = find_seal_boundary(text, window)
        if offset is None:
            continue
        checked_int_results += 1

        sealed = text[:offset].rstrip("\n")
        open_marker = _renderer_open_marker_at_end(sealed)
        assert open_marker is None, (text, window, offset, sealed, open_marker)

    assert checked_int_results > 100
