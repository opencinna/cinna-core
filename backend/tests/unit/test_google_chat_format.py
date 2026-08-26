"""CommonMark → Google Chat markup (`adapters/google_chat_format.py`).

Pure text transformation, no I/O — the sibling of
`test_google_chat_adapter_chunk.py`, which covers the splitting that happens
immediately after this on the same outbound path.

The reason this module exists at all is worth stating once, because it looks
like something a flag should handle: `spaces.messages.create` has **no**
`markupSyntax` parameter. The `MARKUP_SYNTAX_CHAT` / `MARKUP_SYNTAX_MARKDOWN`
enum is read-side only (`spaces.messages.get`), so text posted to Chat is
always interpreted as Chat's own markup and `**bold**` reaches the reader as
four literal asterisks. Translating is the only option.

API-observable behaviour — that a pipeline notice arrives at the adapter in
markdown and leaves it in Chat markup — is covered in
`tests/api/server_channels/server_channels_status_notice_test.py`.
"""
from app.services.server_channels.adapters.google_chat_format import markdown_to_chat


# ---------------------------------------------------------------------------
# Emphasis
# ---------------------------------------------------------------------------


def test_bold_becomes_one_asterisk_not_two() -> None:
    # The reported symptom, verbatim: this text reached a Chat thread with its
    # asterisks visible.
    assert markdown_to_chat(
        "Setting up **MickyJoker - BundleTest** for you — first-time setup "
        "takes a few minutes."
    ) == (
        "Setting up *MickyJoker - BundleTest* for you — first-time setup "
        "takes a few minutes."
    )


def test_underscore_bold_becomes_one_asterisk() -> None:
    assert markdown_to_chat("__strong__ words") == "*strong* words"


def test_italic_asterisk_moves_to_underscore() -> None:
    # The inversion that makes a naive pass-through wrong in BOTH directions:
    # `*x*` is italic in Markdown and BOLD in Chat, so leaving it alone would
    # silently change the emphasis rather than merely fail to apply it.
    assert markdown_to_chat("a *quiet* word") == "a _quiet_ word"


def test_bold_italic_nests_as_chat_spells_it() -> None:
    assert markdown_to_chat("***both***") == "*_both_*"


def test_strikethrough_loses_one_tilde() -> None:
    assert markdown_to_chat("~~gone~~") == "~gone~"


def test_identifiers_with_underscores_are_not_emphasis() -> None:
    # `snake_case` and `file_name.py` are the everyday false positive: an
    # emphasis pass without word-boundary anchors turns half a codebase into
    # italics.
    assert markdown_to_chat("snake_case and file_name.py stay whole") == (
        "snake_case and file_name.py stay whole"
    )


def test_arithmetic_asterisks_are_not_emphasis() -> None:
    assert markdown_to_chat("multiply 2 * 3 * 4 = 24") == "multiply 2 * 3 * 4 = 24"


# ---------------------------------------------------------------------------
# Code — the thing that must NOT be translated
# ---------------------------------------------------------------------------


def test_inline_code_is_left_exactly_alone() -> None:
    # `**kwargs` inside a code span is the case that makes masking mandatory
    # rather than tidy: formatted, it becomes `*kwargs*` and the reader is
    # shown code that would not run.
    assert markdown_to_chat("Use `**kwargs` and `a * b` inline.") == (
        "Use `**kwargs` and `a * b` inline."
    )


def test_fenced_block_keeps_its_body_and_drops_the_info_string() -> None:
    # The language tag goes: Chat has no highlighting and would render the word
    # "python" as the block's first line.
    assert markdown_to_chat("```python\ndef f(**kwargs):\n    return 1 * 2\n```") == (
        "```\ndef f(**kwargs):\n    return 1 * 2\n```"
    )


def test_tilde_fences_become_backtick_fences() -> None:
    assert markdown_to_chat("~~~\nraw ~~text~~\n~~~") == "```\nraw ~~text~~\n```"


# ---------------------------------------------------------------------------
# Block structure Chat does not have
# ---------------------------------------------------------------------------


def test_headings_become_bold_lines() -> None:
    assert markdown_to_chat("# Report\n## Details") == "*Report*\n*Details*"


def test_bold_inside_a_heading_does_not_close_it_early() -> None:
    # Chat does not nest emphasis. Left in, the inner markers terminate the
    # heading's own bold and leave stray asterisks mid-line.
    assert markdown_to_chat("## Heading with **bold**") == "*Heading with bold*"


def test_bullets_normalise_and_keep_their_indent() -> None:
    assert markdown_to_chat("* one\n+ two\n    - nested") == (
        "- one\n- two\n    - nested"
    )


def test_ordered_lists_pass_through_as_literal_text() -> None:
    # Chat has no ordered list. "1. " reads correctly as plain text, which is
    # the whole of the fallback.
    assert markdown_to_chat("1. first\n2. second") == "1. first\n2. second"


def test_a_table_becomes_an_aligned_monospace_block() -> None:
    assert markdown_to_chat(
        "| Name | Count |\n|------|------:|\n| **a** | 1 |\n| bbbb | 22 |"
    ) == "```\nName  Count\na     1\nbbbb  22\n```"


def test_a_horizontal_rule_is_drawn_rather_than_dropped() -> None:
    assert markdown_to_chat("above\n---\nbelow") == "above\n" + "─" * 20 + "\nbelow"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_take_chat_pipe_form() -> None:
    assert markdown_to_chat("See [the docs](https://example.com/x).") == (
        "See <https://example.com/x|the docs>."
    )


def test_a_link_whose_label_is_its_url_stays_bare() -> None:
    assert markdown_to_chat("[https://x.example](https://x.example)") == (
        "https://x.example"
    )


def test_an_image_degrades_to_its_link() -> None:
    assert markdown_to_chat("![a chart](https://x.example/c.png)") == (
        "<https://x.example/c.png|a chart>"
    )


def test_a_link_label_containing_an_angle_bracket_does_not_break_the_link() -> None:
    """The product's own copy does this constantly: "Admin > Channels" as a
    link label.

    Chat's link syntax is ``<url|label>`` with no escape for its own three
    delimiters. Left alone, a ``>`` in the label terminates the link at that
    character — the reader sees "Admin " as the clickable anchor and
    "Channels>" spilled out beside it as literal, un-linked text. The fix
    substitutes the three delimiter characters in the LABEL with lookalikes.
    """
    assert markdown_to_chat("[Admin > Channels](https://x)") == (
        "<https://x|Admin › Channels>"
    )


def test_the_pointy_bracket_url_form_keeps_its_anchor() -> None:
    """CommonMark's ``[t](<url>)`` — and the greedy capture that broke it.

    The URL class used to be ``[^)\\s]+``, which ate the closing ``>`` while
    the optional ``>?`` matched empty. ``_link`` then saw a Chat delimiter
    inside the URL, refused to build an anchor at all, and emitted the bare
    URL **including the stray bracket** — a link that 404s on the ``>``.
    """
    assert markdown_to_chat("See [the docs](<https://example.com/x>).") == (
        "See <https://example.com/x|the docs>."
    )
    # The image form shares the shape and needed the same treatment.
    assert markdown_to_chat("![a chart](<https://x.example/c.png>)") == (
        "<https://x.example/c.png|a chart>"
    )


def test_a_link_url_containing_a_pipe_degrades_to_the_bare_url() -> None:
    """A delimiter in the URL cannot be substituted — that would point the
    link somewhere else — so the whole anchor degrades to the bare URL.

    Naively rendered, ``[a|b](http://x|y)`` becomes ``<http://x|y|a|b>``,
    which Chat resolves to the target ``http://x`` — silently truncating the
    URL the agent actually meant to send. Ugly and correct (the reader gets a
    working, complete link with no label) beats pretty and wrong.
    """
    assert markdown_to_chat("[a|b](http://x|y)") == "http://x|y"


def test_a_literal_nul_or_bold_sentinel_in_agent_output_is_stripped() -> None:
    """The collision case for this module's own mask/sentinel characters.

    ``_convert`` masks code spans behind ``\\x00<index>\\x00`` and parks bold
    behind ``\\x02`` before unmasking at the very end. A literal NUL/STX an
    agent happens to emit is not merely cosmetic if it survives to that
    unmasking step: ``\\x00 0 \\x00`` in the input would be read as "mask
    reference 0" and resolve to whatever span 0 actually is — DUPLICATING an
    unrelated code span into the middle of the message — rather than passing
    through as the literal characters they are. The fix strips both sentinels
    from the input before anything else runs, so by the time masking assigns
    index 0 to the real code span here, there is no stray reference left to
    collide with it.
    """
    text = "Use `real code` and \x00 0 \x00 and \x02 too"
    result = markdown_to_chat(text)

    # The sentinels are gone...
    assert "\x00" not in result
    assert "\x02" not in result
    # ...the literal "0" they surrounded survives as plain text...
    assert " 0 " in result
    # ...and the actual code span was restored exactly once, not duplicated
    # into the sentinel's position as well.
    assert result.count("real code") == 1
    assert result == "Use `real code` and  0  and  too"


# ---------------------------------------------------------------------------
# Tables and fences — edges nothing currently pins
# ---------------------------------------------------------------------------


def test_a_table_with_ragged_rows_renders() -> None:
    """Rows with different cell counts than the header. `_take_table` pads
    each column from what IS present (``default=0`` on the width lookup) and
    must not raise on the short or the long row.
    """
    assert markdown_to_chat(
        "| A | B | C |\n|---|---|---|\n| 1 | 2 |\n| 3 | 4 | 5 | 6 |"
    ) == "```\nA  B  C\n1  2\n3  4  5  6\n```"


def test_an_unclosed_fence_is_closed() -> None:
    """A fenced block with no closing ``` before the input ends. The scan in
    `_take_fenced_block` runs to the end of the lines and the block is masked
    with a closing marker appended regardless — the input is never left with
    an open fence that swallows the rest of a longer message it gets embedded
    into.
    """
    assert markdown_to_chat("before\n```\nunterminated body\nmore body") == (
        "before\n```\nunterminated body\nmore body\n```"
    )


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


def test_plain_text_and_empty_input_pass_through_untouched() -> None:
    assert markdown_to_chat("no markdown at all") == "no markdown at all"
    assert markdown_to_chat("") == ""
    assert markdown_to_chat(None) == ""


def test_a_conversion_failure_returns_the_input_rather_than_raising() -> None:
    """The contract that keeps a formatting bug from becoming a lost message.

    This runs on the outbound path of a webhook transport. A raise here would
    turn "the asterisks look wrong" into "the sender got no answer", so the
    entry point swallows everything and hands back what it was given.

    Asserted with ``is``, not ``==``: ``Hostile`` is a ``str`` subclass, so
    ``== hostile`` passes for ANY string that is character-for-character
    equal — including a freshly built one that merely happens to match. The
    actual contract is that the exact SAME object comes back unmodified.
    """

    class Hostile(str):
        def replace(self, *args: object, **kwargs: object) -> str:  # type: ignore[override]
            raise RuntimeError("boom")

    hostile = Hostile("**text**")
    assert markdown_to_chat(hostile) is hostile
