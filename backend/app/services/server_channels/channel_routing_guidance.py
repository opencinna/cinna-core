"""Guidance replies: what the channel router says when it routes nowhere.

The classifier returns a categorical ``intent`` (``help`` / ``none`` /
``clarify``); it never authors the words a sender reads. This module holds the
two halves of turning that answer into a reply:

- :class:`RoutingGuidance` — **plain data** on ``RoutingDecisionResult``: which
  shape of reply, and the ballot entries it may list. Built inside
  ``ChannelRoutingService.decide`` from the sender's own post-policy ballot
  (their agents, the identities they may address) or from the catalog bundles
  ``CatalogService.user_can_install`` admitted for them — never from anything
  else, so a reply can only name what the sender could already have reached.
  Frozen strings and ids, pinned by ``tests/architecture/
  channel_routing_purity_test.py`` like every other value crossing that
  boundary.
- :func:`compose_guidance_reply` — a pure function from that data to text.
  **No sender text reaches it**: not the message, not a quote, not the model's
  ``message`` / ``reason``. Names and trigger prompts are configuration their
  owners wrote, collapsed to one line and clamped. The markdown it emits is the
  subset ``adapters.google_chat_format.markdown_to_chat`` translates (bold,
  ``- `` bullets, ``1. `` items); email receives it as written.

Nothing here imports a session, a model or a service: the composer is shared by
the effect half (``ChannelInboundService._route_new_thread``) and by
``POST /admin/routing/simulate``, which shows an admin the reply without
sending it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.routing.routing_trace import (
    GUIDANCE_KINDS,
    INTENT_CLARIFY,
    INTENT_HELP,
    INTENT_NONE,
)
from app.services.server_channels.adapters.google_chat_format import (
    LINK_LABEL_SUBSTITUTIONS,
)

#: The reply shapes, by the classifier intent that produces them. The same
#: strings as the intents on purpose: ``stages[].guidance_kind`` on the trace
#: and ``RoutingGuidance.kind`` then read as one vocabulary.
GUIDANCE_HELP = INTENT_HELP
GUIDANCE_NONE = INTENT_NONE
#: Produced by ``decide`` only when its caller passed ``can_clarify`` (a
#: question is only asked where the platform can follow it up); the answer is
#: read by :func:`resolve_choice`.
GUIDANCE_CLARIFY = INTENT_CLARIFY
# ``GUIDANCE_KINDS`` is imported from ``routing_trace``, which coerces the trace
# field to it, and re-exported here so the two cannot hold different sets.

#: What a listed entry is. ``bundle`` entries come only from the catalog pass.
ENTRY_AGENT = "agent"
ENTRY_IDENTITY = "identity"
ENTRY_BUNDLE = "bundle"

#: One-line clamps. Trigger prompts are written for a classifier and can run to
#: paragraphs; a reply listing five of them must stay readable in a chat bubble.
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 160

_WHITESPACE_RE = re.compile(r"\s+")
#: Owner-authored text goes out as the bot, so anything a channel would read as
#: markup is neutralised rather than trusted. Emphasis, code, link-bracket and
#: strike markers are stripped: an unbalanced ``**`` would break the bold around
#: a name, and ``[x](https://…)`` would become a live link through
#: ``markdown_to_chat``. Chat's own delimiters ``<`` ``>`` ``|`` are swapped for
#: look-alikes from the formatter's table, so a raw ``<https://…|x>`` or
#: ``<users/all>`` cannot become a link or an @all mention.
_MARKUP_RE = re.compile(r"[*`\[\]~]")
#: A display name that is an email address: an identity owner with no full
#: name, whom ``IdentityCandidateProvider`` names by email for the classifier.
#: Only the local part is shown.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
#: What an identity entry says instead of its trigger prompt. That prompt is
#: platform-written for the classifier ("Contact <name> (<email>). …") and
#: carries the owner's email address, which a reply must not repeat.
IDENTITY_DESCRIPTION = "passes your message on to one of their assistants"
_NUMBER_WORDS = {2: "two", 3: "three"}


@dataclass(frozen=True)
class GuidanceEntry:
    """One candidate a guidance reply may name. Plain strings only."""

    ref_id: str
    name: str
    description: str
    kind: str


@dataclass(frozen=True)
class RoutingGuidance:
    """The decision routed nowhere, and the sender is to be told this.

    ``entries`` is capped at ``CHANNEL_ROUTING_GUIDANCE_MAX_LISTED`` when built
    (:func:`build_guidance`); ``total`` is how many the ballot held, so the
    reply can say "and N more" without the decision carrying the rest.
    """

    kind: str
    entries: tuple[GuidanceEntry, ...] = ()
    total: int = 0

    @property
    def ref_ids(self) -> tuple[str, ...]:
        """The listed ids, for ``stages[].guidance_options`` on the trace."""
        return tuple(entry.ref_id for entry in self.entries)


def build_guidance(
    kind: str | None, entries: Sequence[GuidanceEntry], *, max_listed: int
) -> RoutingGuidance | None:
    """A capped :class:`RoutingGuidance`, or ``None`` when there is nothing to say.

    ``None`` for an unknown kind or an empty list: a reply that lists nothing is
    ``REPLY_NO_MATCH``, which the caller already sends. ``max_listed`` below one
    is read as one, so a misconfigured setting cannot produce an empty list with
    "and N more" under it.
    """
    if kind not in GUIDANCE_KINDS or not entries:
        return None
    limit = max(1, int(max_listed))
    return RoutingGuidance(
        kind=kind, entries=tuple(entries[:limit]), total=len(entries)
    )


def _one_line(value: str | None, limit: int) -> str:
    """Collapse to one line, neutralise markup, clamp at a word boundary."""
    if not isinstance(value, str):
        return ""
    text = _WHITESPACE_RE.sub(" ", _MARKUP_RE.sub("", value)).strip()
    for delimiter, replacement in LINK_LABEL_SUBSTITUTIONS.items():
        text = text.replace(delimiter, replacement)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-—") + "…"


def safe_display_name(value: str | None) -> str:
    """An owner-authored assistant name, safe to put in a channel message.

    Public because it is not guidance-specific: anything that names an agent
    to a channel reader needs the same three things this does — one line,
    markup neutralised, clamped at a word boundary — and needs them applied
    identically, or the same name reads differently in a guidance list than it
    does in the thread's nameplate. The escaping is the load-bearing part: the
    name goes out as the bot, so an unbalanced ``**`` in it would otherwise
    break the formatting of the message carrying it.

    Total, like the rest of this module: ``None``, a blank name and a name
    that clamps away to nothing all answer "Unnamed assistant" rather than
    putting an empty ``****`` on the thread.
    """
    name = _one_line(value, MAX_NAME_CHARS)
    if _EMAIL_RE.match(name):
        name = name.split("@", 1)[0]
    return name or "Unnamed assistant"


def _entry_line(entry: GuidanceEntry, *, markdown: bool) -> str:
    name = safe_display_name(entry.name)
    if entry.kind == ENTRY_IDENTITY:
        description = IDENTITY_DESCRIPTION
    else:
        description = _one_line(entry.description, MAX_DESCRIPTION_CHARS)
    label = f"**{name}**" if markdown else name
    return f"{label} — {description}" if description else label


def compose_guidance_reply(
    guidance: RoutingGuidance | None, *, markdown: bool = True
) -> str | None:
    """The sender-facing text for ``guidance``, or ``None`` when there is none.

    ``markdown`` is the transport's ``supports_markdown``. Off (email, which
    sends text/plain) the names lose their ``**``; the line breaks, the ``- ``
    bullets and the ``1. `` numbering stay, since they read as plain text too.

    Pure and total over plain data. Three shapes:

    - ``help`` — who the router can hand a message to (or, when every entry is
      a catalog bundle, what it can set up), and how to ask.
    - ``none`` — nothing fit; the same list, and the next move (rephrase, or
      name one).
    - ``clarify`` — a numbered question over the options, best pick first.
    """
    if guidance is None or guidance.kind not in GUIDANCE_KINDS:
        return None
    lines = [_entry_line(entry, markdown=markdown) for entry in guidance.entries]
    if not lines:
        return None
    offers_bundles = all(entry.kind == ENTRY_BUNDLE for entry in guidance.entries)

    if guidance.kind == GUIDANCE_CLARIFY:
        count = _NUMBER_WORDS.get(len(lines), str(len(lines)))
        return "\n".join(
            [
                f"That could go to {count} different assistants:",
                *(f"{index}. {line}" for index, line in enumerate(lines, start=1)),
                "Reply with the number or the name. Anything else and I'll treat "
                "it as a new request.",
            ]
        )

    if guidance.kind == GUIDANCE_HELP:
        if offers_bundles:
            head = (
                "I don't have an assistant set up for you here yet. I can set one up:"
            )
            tail = "Describe what you need and I'll set it up."
        else:
            head = "Here's who I can hand your message to:"
            tail = "Just describe what you need and I'll pick the right one."
    else:
        if offers_bundles:
            head = "I couldn't find an assistant for that. I can set one of these up for you:"
        else:
            head = "I couldn't find an assistant for that. I can hand your message to:"
        tail = "Rephrase your message, or name the one you mean."

    body = [f"- {line}" for line in lines]
    more = guidance.total - len(lines)
    if more > 0:
        body.append(f"…and {more} more.")
    return "\n".join([head, *body, tail])


# --- Reading the answer to a clarifying question ---------------------------


@dataclass(frozen=True)
class ClarificationOption:
    """One option a clarifying question offered, as stored. Plain strings."""

    ref_id: str
    name: str
    kind: str


#: What :func:`resolve_choice` returns when the sender declined every option.
#: Not a valid ref: refs are UUIDs or ``identity:<uuid>``.
CHOICE_CANCEL = "<cancel>"

_CHOICE_SPLIT_RE = re.compile(r"[^\w]+")
_ORDINALS = {
    "1": 1,
    "one": 1,
    "first": 1,
    "1st": 1,
    "2": 2,
    "two": 2,
    "second": 2,
    "2nd": 2,
    "3": 3,
    "three": 3,
    "third": 3,
    "3rd": 3,
}
#: Words a short answer wraps a number or a name in ("option 2", "the second
#: one", "joker please"). Stripped from the ends only, never the middle.
_LEADING_FILLER = frozenset(
    {"i", "choose", "pick", "take", "go", "with", "option", "number", "no", "choice", "the"}
)
_TRAILING_FILLER = frozenset({"one", "option", "please", "pls", "thanks", "thank", "you"})
_CANCEL_PHRASES = frozenset(
    {
        "neither",
        "none",
        "cancel",
        "no",
        "no thanks",
        "no thank you",
        "nope",
        "neither of them",
        "neither of those",
        "none of them",
        "none of these",
        "none of those",
        "never mind",
        "nevermind",
    }
)
#: How many words an answer may add around a name it contains before it stops
#: reading as "that one" and starts reading as a new request that mentions it.
_MAX_WORDS_AROUND_NAME = 2


def _choice_tokens(text: str) -> list[str]:
    return _CHOICE_SPLIT_RE.sub(" ", text.casefold()).split()


def _strip_filler(tokens: list[str]) -> list[str]:
    core = list(tokens)
    while len(core) > 1 and core[0] in _LEADING_FILLER:
        core.pop(0)
    while len(core) > 1 and core[-1] in _TRAILING_FILLER:
        core.pop()
    return core


def _contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    width = len(phrase)
    return any(tokens[i : i + width] == phrase for i in range(len(tokens) - width + 1))


def _single(refs: list[str]) -> str | None:
    """The one ref a matching level found, or ``None`` for none or ambiguity."""
    unique = list(dict.fromkeys(refs))
    return unique[0] if len(unique) == 1 else None


#: Words that turn the name or number beside them into its opposite ("not
#: joker", "don't pick the writer", "anyone except HR"). Matched with
#: apostrophes removed, so "don't" and "dont" are one word.
_NEGATIONS = frozenset({"not", "dont", "isnt", "never", "except"})
_APOSTROPHE_RE = re.compile(r"['’]")


def _negates(reply_text: str) -> bool:
    """Whether a reply refuses what it names instead of choosing it.

    ``no`` is the one word read both ways: before a number it abbreviates
    "number" ("no 2", "no. 3") and stays filler; before any other word, or
    last, it refuses ("no joker", "joker, no"). A bare decline never reaches
    this, because :data:`_CANCEL_PHRASES` is read first.
    """
    tokens = _choice_tokens(_APOSTROPHE_RE.sub("", reply_text))
    if any(token in _NEGATIONS for token in tokens):
        return True
    return any(
        token == "no"
        and (index + 1 == len(tokens) or not _is_number(tokens[index + 1]))
        for index, token in enumerate(tokens)
    )


def _is_number(token: str) -> bool:
    return token.isdigit() or token in _ORDINALS


def resolve_choice(
    reply_text: str | None, options: Sequence[ClarificationOption]
) -> str | None:
    """Which option a reply to a clarifying question names, if it plainly names one.

    Returns the chosen ``ref_id``, :data:`CHOICE_CANCEL`, or ``None`` when the
    reply does not plainly answer (the caller then asks the classifier over the
    options). Pure and total. Case-, whitespace- and punctuation-insensitive.
    Levels, first match wins, and an ambiguous level answers ``None`` rather
    than guessing:

    1. A decline, read on the whole reply before any filler is stripped:
       ``neither``, ``none``, ``cancel``, ``no``, ``no thanks`` and close
       variants.
    2. A negation answers ``None``, whatever it names: ``not``, ``don't``,
       ``isn't``, ``never``, ``except``, or ``no`` before anything but a
       number ("not joker", "no, the writer"). ``no 2`` is still option 2.
    3. An ordinal within range: ``2``, ``#2``, ``2.``, ``option 2``, ``second``,
       ``the second one``.
    4. An exact name, as the question displayed it.
    5. A prefix of one name, then a substring of one name, each at least 3
       characters (so "hi" never selects "Hiring").
    6. A short answer containing one whole name: ``the joker one``. At most
       :data:`_MAX_WORDS_AROUND_NAME` extra words, so "write a joke about the
       writer" is not read as choosing Writer.
    """
    if not isinstance(reply_text, str) or not options:
        return None
    tokens = _choice_tokens(reply_text)
    if not tokens:
        return None
    reply = " ".join(tokens)
    if reply in _CANCEL_PHRASES:
        return CHOICE_CANCEL
    if _negates(reply_text):
        # Ahead of every level that finds a name or a number: "no" is leading
        # filler and a name may carry two extra words, so each of them would
        # read "no joker" or "not the writer" as picking that option. A short
        # negation goes on to the classifier, which can read what is wanted.
        return None
    core = _strip_filler(tokens)

    if len(core) == 1:
        index = _ORDINALS.get(core[0])
        if index is not None and index <= len(options):
            return options[index - 1].ref_id

    names = [
        (option.ref_id, _choice_tokens(safe_display_name(option.name)))
        for option in options
    ]
    exact = [ref for ref, name in names if name and " ".join(name) == reply]
    if exact:
        return _single(exact)

    core_text = " ".join(core)
    # A lone filler word ("no", "the", "one") is not the start of a name.
    if core_text in _LEADING_FILLER or core_text in _TRAILING_FILLER:
        return None
    if len(core_text) >= 3:
        prefix = [ref for ref, name in names if " ".join(name).startswith(core_text)]
        if prefix:
            return _single(prefix)
        substring = [ref for ref, name in names if core_text in " ".join(name)]
        if substring:
            return _single(substring)

    contained = [
        ref
        for ref, name in names
        if name
        and len(tokens) <= len(name) + _MAX_WORDS_AROUND_NAME
        and _contains_phrase(tokens, name)
    ]
    return _single(contained) if contained else None


#: The longest reply, in words after the filler :func:`resolve_choice` ignores,
#: that may still be read as picking an option when no rule matched it. Longer
#: replies are new requests, as the question promises ("Anything else and I'll
#: treat it as a new request"), and are never classified over the options.
CLARIFY_SELECTOR_MAX_WORDS = 4


def is_selector_like(reply_text: str | None) -> bool:
    """Whether an unread reply is short enough to be an attempt at choosing.

    ``True`` only for a non-empty reply of at most
    :data:`CLARIFY_SELECTOR_MAX_WORDS` words once leading and trailing filler
    ("option", "the", "please") is stripped. Pure and total.
    """
    if not isinstance(reply_text, str):
        return False
    tokens = _choice_tokens(reply_text)
    return bool(tokens) and len(_strip_filler(tokens)) <= CLARIFY_SELECTOR_MAX_WORDS


__all__ = [
    "CHOICE_CANCEL",
    "CLARIFY_SELECTOR_MAX_WORDS",
    "ENTRY_AGENT",
    "ENTRY_BUNDLE",
    "ENTRY_IDENTITY",
    "GUIDANCE_CLARIFY",
    "GUIDANCE_HELP",
    "GUIDANCE_KINDS",
    "GUIDANCE_NONE",
    "IDENTITY_DESCRIPTION",
    "MAX_DESCRIPTION_CHARS",
    "MAX_NAME_CHARS",
    "ClarificationOption",
    "GuidanceEntry",
    "RoutingGuidance",
    "build_guidance",
    "compose_guidance_reply",
    "is_selector_like",
    "resolve_choice",
    "safe_display_name",
]
