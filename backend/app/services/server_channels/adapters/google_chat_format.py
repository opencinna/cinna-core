"""CommonMark → Google Chat markup.

Agents answer in Markdown. Google Chat's ``text`` field is **not** Markdown:
it has its own small markup language, and ``spaces.messages.create`` offers no
way to say otherwise — the ``markupSyntax`` enum
(``MARKUP_SYNTAX_CHAT`` / ``MARKUP_SYNTAX_MARKDOWN``) exists only as a
*read-side* parameter on ``spaces.messages.get``. So ``**bold**`` reaches the
reader as four literal asterisks, and there is no flag that fixes it. This
module is the translation.

The mapping, and where it is lossy:

===================  ==========================  =======================
Markdown             Chat                        Note
===================  ==========================  =======================
``**b**`` ``__b__``  ``*b*``                     one asterisk, not two
``*i*``              ``_i_``                     ``*`` means BOLD in Chat
``_i_``              ``_i_``                     already correct
``~~s~~``            ``~s~``
```` `c` ````        ```` `c` ````               unchanged
fenced block         fenced block                info string dropped
``[t](u)``           ``<u|t>``
``# H`` … ``###### H``  ``*H*``                  Chat has no headings
``- x`` / ``* x``    ``- x``
``1. x``             ``1. x``                    literal; Chat has no <ol>
``---``              a rule of box-drawing dashes
table                monospace block             Chat has no tables
===================  ==========================  =======================

Two things are load-bearing rather than tidy, and both are about *not*
translating:

* **Code is masked first and restored last.** Fenced blocks and inline spans
  are lifted out before any substitution runs and put back after. Without that
  a snippet containing ``**kwargs`` or ``a * b`` gets "formatted", which is
  worse than the literal asterisks this module exists to remove.
* **Emphasis needs word boundaries.** ``snake_case``, ``file_name.py`` and
  ``__init__`` are not italics. Every emphasis pattern below is anchored on
  non-word delimiters, and the underscore forms additionally refuse to match
  when a word character sits against the marker.
"""
from __future__ import annotations

import re

# Sentinels for masked spans, and for the bold placeholder further down. Both
# are control characters an agent has no reason to emit — but "no reason to" is
# not "cannot", and a collision here does not degrade gracefully: a literal
# ``\x00 0 \x00`` in the input resolves against the mask table and DUPLICATES
# whatever span 0 happens to be, while a literal ``\x02`` comes out as a stray
# asterisk. So ``_convert`` strips both from the input before anything else
# runs, and from then on every occurrence is one this module put there.
_MASK_OPEN = "\x00"
_MASK_CLOSE = "\x00"
_MASK_RE = re.compile(r"\x00(\d+)\x00")

#: Public because ``chat_text_chunking`` shares this model of "what is a
#: fence line" when it scans RAW markdown: a seal boundary must never land
#: inside a block this module would still consider open. Note the two
#: groups a consumer needs: group 2 is the marker run, and its first
#: character is the marker *type* — a closing fence must repeat the
#: opening one (see ``_take_fenced_block``), so a ``~~~`` line inside a
#: backtick block is content, not a close.
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`~]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE_RE = re.compile(r"^\s{0,3}(?:-\s*-\s*-[-\s]*|\*\s*\*\s*\*[*\s]*|_\s*_\s*_[_\s]*)$")
_BULLET_RE = re.compile(r"^(\s*)[*+-][ \t]+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)][ \t]+(.*)$")
#: Public because ``chat_text_chunking`` shares this model of "what is a
#: table row": the seal boundary it picks has to agree with what this
#: module will actually render, so a rename here has a consumer.
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)

# Chat renders a horizontal rule as nothing, so draw one.
_RULE_TEXT = "─" * 20

# --- Inline emphasis -------------------------------------------------------
#
# Order matters: the three-marker form first, then the two-marker form, then
# the single. Bold results are parked on a placeholder (`\x02`) so the italic
# pass — which turns a lone `*` into `_` — cannot see the asterisk bold just
# produced.
_BOLD_SENTINEL = "\x02"

# The URL class excludes ``>`` so the CommonMark pointy-bracket form —
# ``[t](<http://x>)`` — does not capture the closing bracket as part of the
# URL. It used to: ``[^)\s]+`` ate the ``>`` and the optional ``>?`` matched
# empty, so ``_link`` saw a delimiter in the URL and degraded the whole thing
# to a bare URL *including* the stray bracket. Nothing legitimate is lost —
# ``_link`` refuses any URL containing ``>`` anyway.
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

_BOLD_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*(?!\*)")
_BOLD_STAR_RE = re.compile(r"(?<!\*)\*\*(?!\s)(.+?)(?<!\s)\*\*(?!\*)")
_ITALIC_STAR_RE = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_BOLD_ITALIC_US_RE = re.compile(r"(?<![\w_])___(?!\s)(.+?)(?<!\s)___(?![\w_])")
_BOLD_US_RE = re.compile(r"(?<![\w_])__(?!\s)(.+?)(?<!\s)__(?![\w_])")
_STRIKE_RE = re.compile(r"(?<!~)~~(?!\s)(.+?)(?<!\s)~~(?!~)")


def markdown_to_chat(text: str | None) -> str:
    """Render CommonMark ``text`` in Google Chat's markup. Never raises.

    Total by contract: this sits on the outbound path of a webhook transport,
    where a formatting bug must degrade to "ugly message" and never to "no
    message". Anything unexpected returns the input unchanged.
    """
    if not text:
        return text or ""
    try:
        return _convert(text)
    except Exception:  # noqa: BLE001 — see the docstring
        return text


def _convert(text: str) -> str:
    # The two sentinels, removed before they can be confused with this
    # module's own. See the comment on ``_MASK_OPEN``: a NUL that survives to
    # ``_unmask`` is read as a mask delimiter and resolves to some other
    # span's text, which is a duplication rather than the pass-through the
    # collision case is supposed to degrade to.
    text = text.replace(_MASK_OPEN, "").replace(_BOLD_SENTINEL, "")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    masked: list[str] = []
    out: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index]

        fence = FENCE_RE.match(line)
        if fence is not None:
            index = _take_fenced_block(lines, index, fence, masked, out)
            continue

        if _TABLE_DIVIDER_RE.match(line) and out and TABLE_ROW_RE.match(lines[index - 1]):
            # The header row was already emitted as an ordinary line; take it
            # back and re-render the whole table as one monospace block.
            out.pop()
            index = _take_table(lines, index - 1, masked, out)
            continue

        out.append(_convert_line(line, masked))
        index += 1

    return _unmask("\n".join(out), masked)


def _take_fenced_block(
    lines: list[str],
    index: int,
    fence: re.Match[str],
    masked: list[str],
    out: list[str],
) -> int:
    """Emit lines[index:] up to the closing fence as one masked block."""
    body: list[str] = []
    index += 1
    while index < len(lines):
        closing = FENCE_RE.match(lines[index])
        if closing is not None and closing.group(2)[0] == fence.group(2)[0]:
            index += 1
            break
        body.append(lines[index])
        index += 1
    # Always backticks, whatever the source used: Chat knows no other fence
    # marker, so a ``~~~`` block re-emitted with tildes would reach the reader
    # as three literal tildes wrapped around unformatted text.
    #
    # The info string (```python) is dropped too: Chat has no syntax
    # highlighting and would show the word as the block's first line.
    marker = "```"
    out.append(_mask("\n".join([marker, *body, marker]), masked))
    return index


def _take_table(lines: list[str], index: int, masked: list[str], out: list[str]) -> int:
    """Render a pipe table as an aligned monospace block.

    Chat has no table markup, and the alternative — dropping the pipes — makes
    a column of numbers unreadable. A monospace block at least keeps them
    lined up.
    """
    rows: list[list[str]] = []
    while index < len(lines) and TABLE_ROW_RE.match(lines[index]):
        raw = lines[index].strip().strip("|")
        index += 1
        if _TABLE_DIVIDER_RE.match(f"|{raw}|"):
            continue
        rows.append([_strip_inline_markers(cell.strip()) for cell in raw.split("|")])

    if not rows:
        return index

    width = max(len(row) for row in rows)
    widths = [
        max((len(row[col]) for row in rows if col < len(row)), default=0)
        for col in range(width)
    ]
    rendered = [
        "  ".join(
            (row[col] if col < len(row) else "").ljust(widths[col])
            for col in range(width)
        ).rstrip()
        for row in rows
    ]
    out.append(_mask("\n".join(["```", *rendered, "```"]), masked))
    return index


def _convert_line(line: str, masked: list[str]) -> str:
    heading = _HEADING_RE.match(line)
    if heading is not None:
        # The whole heading becomes bold, so any bold *inside* it has to go:
        # Chat does not nest emphasis, and the stray markers would close the
        # heading early and leave visible asterisks in the middle of it.
        body = _convert_inline(heading.group(2), masked).replace("*", "")
        return f"*{body}*" if body else ""

    if _RULE_RE.match(line):
        return _RULE_TEXT

    bullet = _BULLET_RE.match(line)
    if bullet is not None:
        return f"{bullet.group(1)}- {_convert_inline(bullet.group(2), masked)}"

    ordered = _ORDERED_RE.match(line)
    if ordered is not None:
        # Chat has no ordered lists; the literal "1. " reads correctly.
        body = _convert_inline(ordered.group(3), masked)
        return f"{ordered.group(1)}{ordered.group(2)}. {body}"

    return _convert_inline(line, masked)


def _convert_inline(text: str, masked: list[str]) -> str:
    if not text:
        return text

    # Code spans are masked before anything else can reformat their contents.
    text = _INLINE_CODE_RE.sub(
        lambda m: _mask(f"{m.group(1)}{m.group(2)}{m.group(1)}", masked), text
    )

    text = _IMAGE_RE.sub(lambda m: _link(m.group(2), m.group(1)), text)
    text = _LINK_RE.sub(lambda m: _link(m.group(2), m.group(1)), text)

    text = _BOLD_ITALIC_STAR_RE.sub(
        lambda m: f"{_BOLD_SENTINEL}_{m.group(1)}_{_BOLD_SENTINEL}", text
    )
    text = _BOLD_ITALIC_US_RE.sub(
        lambda m: f"{_BOLD_SENTINEL}_{m.group(1)}_{_BOLD_SENTINEL}", text
    )
    text = _BOLD_STAR_RE.sub(lambda m: f"{_BOLD_SENTINEL}{m.group(1)}{_BOLD_SENTINEL}", text)
    text = _BOLD_US_RE.sub(lambda m: f"{_BOLD_SENTINEL}{m.group(1)}{_BOLD_SENTINEL}", text)
    # Single `*` is italic in Markdown and BOLD in Chat, so it has to move to
    # `_`. Runs after the bold passes, which have parked their asterisks on the
    # sentinel and so cannot be re-read here.
    text = _ITALIC_STAR_RE.sub(lambda m: f"_{m.group(1)}_", text)
    text = _STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", text)

    return text.replace(_BOLD_SENTINEL, "*")


#: Chat's link syntax is ``<url|label>`` and offers no escape for its own
#: three delimiters, so a label carrying one has to be rewritten. ``>`` is the
#: case that actually happens: this product's own copy says "Admin > Server
#: Configuration > Channels" constantly, and left alone Chat terminates the
#: link at the first ``>`` — the reader sees "Admin " as the anchor and
#: ``Channels>`` spilled out beside it as literal text.
_LINK_LABEL_SUBSTITUTIONS = {"<": "\u2039", ">": "\u203a", "|": "\u00a6"}


def _link(url: str, label: str) -> str:
    """``<url|label>``, or the bare URL when a real anchor is impossible.

    The label is sanitised (see ``_LINK_LABEL_SUBSTITUTIONS``); a *URL*
    carrying a delimiter cannot be — rewriting it would point the link
    somewhere else — so such a link degrades to the bare URL. Ugly and
    correct beats pretty and wrong: ``[a|b](http://x|y)`` rendered naively is
    ``<http://x|y|a|b>``, which Chat resolves to the target ``http://x``.
    """
    label = (label or "").strip()
    if not label or label == url:
        return url
    if any(delimiter in url for delimiter in _LINK_LABEL_SUBSTITUTIONS):
        return url
    for delimiter, replacement in _LINK_LABEL_SUBSTITUTIONS.items():
        label = label.replace(delimiter, replacement)
    return f"<{url}|{label}>"


def _strip_inline_markers(cell: str) -> str:
    """Drop emphasis markers inside a table cell.

    The cell is about to land inside a monospace block, where Chat renders
    markup literally — so translating ``**Total**`` to ``*Total*`` would only
    trade one set of visible asterisks for another.
    """
    cell = _LINK_RE.sub(lambda m: m.group(1), cell)
    for marker in ("***", "___", "**", "__", "~~"):
        cell = cell.replace(marker, "")
    return cell


def _mask(value: str, masked: list[str]) -> str:
    masked.append(value)
    return f"{_MASK_OPEN}{len(masked) - 1}{_MASK_CLOSE}"


def _unmask(value: str, masked: list[str]) -> str:
    def restore(match: re.Match[str]) -> str:
        position = int(match.group(1))
        if 0 <= position < len(masked):
            return masked[position]
        return match.group(0)

    # Masked blocks can nest (an inline span inside a table cell), so resolve
    # until the text stops changing rather than in one pass.
    for _ in range(5):
        replaced = _MASK_RE.sub(restore, value)
        if replaced == value:
            break
        value = replaced
    return value


__all__ = ["markdown_to_chat"]
