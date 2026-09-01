"""Fence-aware splitting of chat text at a per-message size limit.

Extracted from ``GoogleChatAdapter`` so the two consumers that must agree on
"where may a Chat message be cut" share one implementation instead of two that
drift:

* :func:`chunk_text` — the adapter's hard split for an already-final reply
  that exceeds the transport's per-message cap (``send_message`` /
  ``replace_message``).
* :func:`find_seal_boundary` — the streaming relay's *seal* decision: given a
  growing draft, where can it be cut so the sealed part reads as a finished
  message and the remainder continues in a fresh one. Unlike the hard split it
  is allowed to answer "nowhere yet" — the relay simply keeps growing the
  draft and asks again — so it can insist on better boundaries (a blank line,
  never inside a fence, and — unless the alternative is not sealing at all —
  never between the rows of one table) than a forced split can afford to.

**The two live in different text spaces, and therefore carry two different —
both correct — models of "what is a fence line". Do not unify them.**

* :func:`chunk_text` / :func:`fence_open_after` run on **translated** text: the
  adapter chunks the *output* of ``markdown_to_chat``, and that renderer
  normalises every fenced block it emits to ```` ``` ```` whatever the source
  used (``_take_fenced_block``). Backtick-only detection is exactly right
  there, and widening it to the renderer's own ``FENCE_RE`` would be a bug: a
  literal ``~~~`` line surviving as *content* inside a rendered backtick block
  would start toggling state that the reader's Chat client never toggles.
* :func:`find_seal_boundary` runs on **raw markdown**: the streaming relay
  seals source slices *before* translation. There ``~~~`` fences are real, so
  the scan mirrors the renderer's grammar — ``FENCE_RE`` plus the rule that a
  closing fence must repeat the opening marker character.

What both models answer is the same question — where may the *rendered*
message end — asked about the text each one actually holds.

None of the three functions raises: they are called from outbound delivery
paths whose discipline is documented at length in ``channel_outbound_service``
(never let a formatting decision fail a delivery).
"""
from __future__ import annotations

# The renderer's own pipe-row pattern, imported rather than re-declared: this
# module exists to stop two copies of "where may a Chat message be cut" from
# drifting, and a third copy of the table model would be the same mistake one
# level down. ``google_chat_format`` imports nothing from this package, so
# there is no cycle.
from app.services.server_channels.adapters.google_chat_format import (
    FENCE_RE,
    TABLE_ROW_RE,
)


def fence_open_after(piece: str, open_before: bool) -> bool:
    """Whether a fenced code block is still open at the end of ``piece``.

    **Translated-text fence model.** ``piece`` has already been through
    ``markdown_to_chat``, which re-emits every fenced block with a bare
    ```` ``` ```` marker and no info string, so a plain backtick toggle is the
    complete model here — and the *only* correct one. Deliberately NOT the
    renderer's ``FENCE_RE``: see the module docstring, and
    :func:`find_seal_boundary` for the raw-markdown counterpart that is right
    to be different.
    """
    state = open_before
    for line in piece.split("\n"):
        if line.lstrip().startswith("```"):
            state = not state
    return state


def chunk_text(text: str, limit: int) -> list[str]:
    """Split at the message limit, preferring a newline boundary.

    Code fences are closed and re-opened across the split. A ```````
    block cut in half leaves the first chunk with an unterminated fence —
    Chat renders the rest of that message as prose — and the second chunk
    opening with the block's *closing* fence, which then swallows whatever
    follows it. The reserve below is what keeps re-opening the fence from
    pushing the chunk back over the limit.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    # Room for a closing "\n```" on the chunk we cut and an opening
    # "```\n" on the next. Reserved for the whole split rather than per
    # chunk, because whether a given chunk needs one fence, both, or
    # neither is only known after it has been cut — and not reserved at all
    # for text that has no fences in it, so ordinary prose splits exactly
    # where it always did.
    reserve = 8 if "```" in text else 0
    # A limit too small to hold even the fence bookkeeping has no split that
    # satisfies it, and the loop below would spin forever trying (a negative
    # window slices from the END of the string, so ``remaining`` stops
    # shrinking). Unreachable from the adapter, which always passes
    # ``_MAX_MESSAGE_CHARS``; guarded because the limit is now a parameter.
    if limit - reserve <= 0:
        return [text]
    open_fence = False
    # ``limit - reserve``, not ``limit``: the loop condition decides how
    # big the FINAL chunk may be, and that chunk gets the re-opening
    # "```\n" prepended like any other. Exiting at ``limit`` let a tail of
    # exactly ``limit`` characters become ``limit + 4`` — Chat answers 400,
    # the adapter's retry helper gives up immediately on a non-429 4xx, and
    # the earlier chunks are already posted, so the end of a long reply
    # vanishes AND the binding records a delivery failure for an answer the
    # reader mostly received.
    while len(remaining) > limit - reserve:
        window_size = limit - reserve
        window = remaining[:window_size]
        split_at = window.rfind("\n")
        # Only honour a newline if it isn't pathologically early, otherwise
        # a long unbroken line would produce a stream of tiny chunks.
        if split_at < window_size // 2:
            split_at = window_size
        piece = remaining[:split_at].rstrip("\n")
        remaining = remaining[split_at:].lstrip("\n")

        was_open = open_fence
        open_fence = fence_open_after(piece, was_open)
        if was_open:
            piece = f"```\n{piece}"
        if open_fence:
            piece = f"{piece}\n```"
        chunks.append(piece)
    if remaining:
        chunks.append(f"```\n{remaining}" if open_fence else remaining)
    return chunks


def find_seal_boundary(text: str, window: int) -> int | None:
    """The best offset to cut ``text[:offset]`` as a finished, sealed message.

    A boundary is a newline lying strictly inside ``text[:window]`` that leaves
    at least ``window // 2`` characters to seal — the same floor
    :func:`chunk_text` applies when it refuses a pathologically early newline,
    and for the same reason: a message the reader would call "one sentence"
    is not worth a message of its own. Among the boundaries that clear it, the
    candidates in order of preference are:

    1. the last **paragraph break** (a content line followed by a blank line)
       outside a code fence,
    2. the last **line break** outside a code fence.

    The floor is applied *within* each tier, never across them: a paragraph
    break below the floor does not beat a line break above it, and a line break
    is never promoted over a paragraph break that also clears the floor.

    A cut that would land between two consecutive rows of one pipe table is
    walked back to just above the table, because a table split across two
    messages renders as two separately aligned monospace blocks. The split is
    taken anyway when backing out is not an improvement: the table starts at
    the very beginning of the text, backing out would leave a sealed slice
    below the floor, or everything above the table is blank (there would be
    nothing to seal). An unboundedly deferred seal, or a two-word one, is worse
    than two monospace blocks.

    Returns ``None`` when there is no acceptable boundary, which happens when:

    * ``window <= 0``, or the text still fits (``len(text) <= window``) — there
      is nothing to seal off yet;
    * no candidate newline lies inside the window — one unbroken construct (a
      single paragraph, or an open fence) fills it. Note that a *table* filling
      the window does NOT produce ``None``: its row ends are line breaks, and
      with nothing above the table to back out to, the split is taken;
    * every candidate is below the floor — the draft's only boundaries are in
      its first half, the ``"Sure! Here's the answer.\\n\\n" + <3300 chars>``
      shape. Nothing seals *there*; a boundary in the second half arrives with
      later text and seals then;
    * the preferred candidate — the last paragraph break in the window, or the
      last line break when there is none — leaves an empty remainder (only
      newlines follow it). Earlier candidates are not retried; deferring is
      safe, since the draft keeps growing.

    ``None`` means "don't seal yet": the caller keeps growing the draft and
    either a boundary arrives with later text or the transport's hard cap
    forces a :func:`chunk_text`-style split at delivery. It deliberately does
    NOT mean "cut anyway" — this function's contract is that a sealed message
    always ends at a place a reader would accept as an ending.

    The returned offset points just *after* the boundary newline and the run of
    newlines following it, so ``text[:offset].rstrip("\\n")`` is the sealed
    message and ``text[offset:]`` starts the next one without the blank line
    that separated them. Only newline characters are consumed — a
    whitespace-only line survives into the remainder. Guarantees on the result:

    * ``text[offset:]`` is never empty (it may be whitespace — the draft it
      continues is still growing);
    * ``text[:offset]`` always contains non-whitespace;
    * the sealed content — ``text[:offset].rstrip("\\n")``, which is what the
      floor is measured on — is at least ``window // 2`` characters and shorter
      than ``window`` (the boundary newline is strictly inside it), though
      ``offset`` itself may run past ``window`` by the run of newlines it
      consumed.
    """
    if window <= 0 or len(text) <= window:
        return None

    floor = window // 2
    best_paragraph: int | None = None
    best_line: int | None = None
    # **Raw-markdown fence model**, mirroring ``markdown_to_chat``'s
    # ``_take_fenced_block``: a fence line is one that is *only* a marker run
    # (```` ``` ```` or ``~~~``) plus an optional whitespace-free info
    # string, and a block is closed only by a fence repeating the *same marker
    # character* it opened with. So this tracks the open marker rather than
    # toggling a bool — inside a ``~~~`` block a ```` ``` ```` line is content,
    # and vice versa.
    #
    # Deliberately NOT :func:`fence_open_after`, whose backtick-only toggle is
    # the right model for the *translated* text it is given and the wrong one
    # here; see the module docstring before merging the two.
    open_marker: str | None = None
    start = 0
    # Walked by index rather than ``text.split("\n")``: the relay calls this
    # every few seconds on a buffer that grows all turn, and only the first
    # ``window`` characters can hold a boundary — there is no reason to
    # materialise the whole draft as a list of lines to look at its head. Every
    # read below is bounded by the window for the same reason.
    while True:
        # Index of the newline terminating this line. ``-1`` means no newline
        # left inside the window — either the final line, which has no newline
        # to cut at, or a line running past the window.
        line_end = text.find("\n", start, window)
        if line_end == -1:
            break
        line = text[start:line_end]
        fence = FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group(2)[0]
            if open_marker is None:
                open_marker = marker
            elif marker == open_marker:
                open_marker = None
        # The newline at ``line_end`` is a candidate cut when we are outside
        # any fence, the slice it would seal clears the floor, and this line
        # has content (cutting after a *closing* fence line is fine — the
        # state update above already ran, so the state is the one that holds
        # AFTER this line). Blank lines are not candidates themselves; they
        # turn the newline before them into a paragraph break instead.
        if open_marker is None and line_end >= floor and line.strip():
            best_line = line_end
            if _next_line_is_blank(text, line_end):
                best_paragraph = line_end
        start = line_end + 1

    scan_boundary = best_paragraph if best_paragraph is not None else best_line
    if scan_boundary is None:
        return None

    boundary = _back_out_of_table(text, scan_boundary, window)
    end = _after_boundary(text, boundary)
    if end is not None:
        return end
    if boundary != scan_boundary:
        # The walk-back gave up everything above the table — the draft opens
        # with the table, preceded by nothing but blank lines. The table split
        # it was avoiding is still a legal cut, and a legal cut beats telling
        # the caller there is none. The fallback cannot fail in turn: the scan
        # loop only proposes a boundary with a content line above it (nothing
        # blank to seal), and the walk-back only ran because a table row —
        # which contains a "|", not a newline — follows that boundary
        # immediately (so the remainder is non-empty).
        return _after_boundary(text, scan_boundary)
    return None


def _next_line_is_blank(text: str, line_end: int) -> bool:
    """Whether the line starting just after the newline at ``line_end`` is blank.

    Scans the whitespace run rather than locating the next line's end: the
    answer is settled by the first non-whitespace character, so the read is
    bounded by that run instead of by the length of the line — and the relay
    asks this about a draft whose tail grows all turn. A whitespace-only tail
    with no terminating newline counts as blank, matching the slice-and-strip
    this replaces.
    """
    index = line_end + 1
    while index < len(text):
        char = text[index]
        if char == "\n":
            return True
        if not char.isspace():
            return False
        index += 1
    return True


def _after_boundary(text: str, boundary: int) -> int | None:
    """Offset just past the boundary newline run, or ``None`` if unusable.

    Consuming the run is what keeps the blank line that separated the two
    halves out of both of them. Only newline characters count as the run: a
    whitespace-only line survives into the remainder (cosmetic, and rare
    enough not to be worth a second notion of "blank"). ``None`` means the cut
    is not worth making: nothing but newlines follows it (no remainder to
    continue), or nothing but whitespace precedes it (nothing to seal).
    """
    end = boundary
    while end < len(text) and text[end] == "\n":
        end += 1
    if end >= len(text):
        return None
    if not text[:end].strip():
        return None
    return end


def _back_out_of_table(text: str, boundary: int, window: int) -> int:
    """Move a boundary that would split a pipe table to just above the table.

    A cut *between table rows* is exactly: the line ending at ``boundary`` and
    the line immediately after it are both table rows. "Immediately" is
    load-bearing — a blank line between them ends the table as far as markdown
    (and ``markdown_to_chat``'s own table detection) is concerned, so two
    tables separated by a blank line are two tables and cutting between them is
    the ideal boundary, not a split.

    Walking back to just above the table's first row keeps the table whole in
    the next message. ``boundary`` is returned unchanged — accepting the split
    — when the boundary is not inside a table at all, when the table starts at
    the very beginning of ``text`` (nothing above it to seal), or when the
    walked-back slice would fall below :func:`find_seal_boundary`'s
    ``window // 2`` floor: two monospace blocks beat a seal that keeps being
    deferred, and beat a two-word one too.

    The table test is deliberately looser than ``markdown_to_chat``'s, which
    also requires the ``|---|`` divider row: a false positive costs one
    slightly earlier seal, a false negative costs a table rendered as two
    misaligned blocks — and worse, a second half that has lost the header and
    divider the renderer needs to recognise a table at all, leaving raw pipes.
    """
    line_start = text.rfind("\n", 0, boundary) + 1
    before = text[line_start:boundary]

    # Read at most ``window`` characters of the following line rather than
    # slicing to the next newline, which on an unterminated final line copies
    # the whole growing tail. Truncation can only make ``TABLE_ROW_RE`` fail —
    # the pattern needs a trailing "|" — so the boundary is accepted, and a
    # single table row longer than the entire seal window could not have been
    # kept whole in the next message anyway.
    after_stop = min(len(text), boundary + 1 + window)
    after_end = text.find("\n", boundary + 1, after_stop)
    if after_end == -1:
        after_end = after_stop
    after = text[boundary + 1 : after_end]

    if not (TABLE_ROW_RE.match(before) and TABLE_ROW_RE.match(after)):
        return boundary

    # Walk up to the first row of this table.
    table_start = line_start
    while table_start > 0:
        prev_start = text.rfind("\n", 0, table_start - 1) + 1
        prev_line = text[prev_start : table_start - 1]
        if not TABLE_ROW_RE.match(prev_line):
            break
        table_start = prev_start

    if table_start == 0:
        # The table is all there is; splitting it is the only way to seal.
        # (The floor below would reject it too, but for a reason that reads
        # like an accident rather than the fact that there is nothing above.)
        return boundary
    # ``table_start - 1`` is the newline above the table's first row; the
    # caller consumes it (and any blank line it ends) when it walks forward.
    # The floor is measured on what would actually be sealed — the same
    # ``rstrip("\n")`` the caller applies — because a blank line above the
    # table makes the slice shorter than the offset suggests, which is
    # precisely the case where the difference decides.
    if len(text[: table_start - 1].rstrip("\n")) < window // 2:
        return boundary  # accept the table split; see the docstring
    return table_start - 1


__all__ = ["chunk_text", "fence_open_after", "find_seal_boundary"]
