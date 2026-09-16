"""`channel_routing_guidance` — the guidance reply composer, as pure data → text.

Channel routing guidance, Phase 2. The classifier picks a *shape* (`help` /
`none` / `clarify`); these two functions decide every word the sender reads:
`build_guidance` caps the ballot into a `RoutingGuidance`, and
`compose_guidance_reply` turns that into text. Nothing sender-authored reaches
either (the composer takes no message argument at all), so what is pinned here
is the owner-authored side: names and trigger prompts are collapsed to one line,
clamped, and stripped of anything that would render as markup, a link or a
mention in Google Chat.

The API-observable half — which entries a real decision lists, the kill switch,
email getting plain text, the trace and debug feed — is in
`tests/api/server_channels/server_channels_routing_guidance_test.py`, and the
simulate half in `tests/api/routing/routing_simulate_guidance_reply_test.py`.

No DB, no Docker, no LLM calls.
"""
import pytest

from app.services.routing import routing_trace
from app.services.server_channels.channel_routing_guidance import (
    ENTRY_AGENT,
    ENTRY_BUNDLE,
    ENTRY_IDENTITY,
    GUIDANCE_CLARIFY,
    GUIDANCE_HELP,
    GUIDANCE_KINDS,
    GUIDANCE_NONE,
    IDENTITY_DESCRIPTION,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    GuidanceEntry,
    RoutingGuidance,
    build_guidance,
    safe_display_name,
    compose_guidance_reply,
)


def _agent(name: str, description: str = "", ref_id: str | None = None) -> GuidanceEntry:
    return GuidanceEntry(
        ref_id=ref_id or f"agent-{name}", name=name, description=description, kind=ENTRY_AGENT
    )


def _bundle(name: str, description: str = "") -> GuidanceEntry:
    return GuidanceEntry(
        ref_id=f"bundle-{name}", name=name, description=description, kind=ENTRY_BUNDLE
    )


def _identity(name: str, description: str = "") -> GuidanceEntry:
    return GuidanceEntry(
        ref_id=f"identity:{name}", name=name, description=description, kind=ENTRY_IDENTITY
    )


def _compose(kind: str, *entries: GuidanceEntry, max_listed: int = 5, **kwargs) -> str | None:
    return compose_guidance_reply(
        build_guidance(kind, list(entries), max_listed=max_listed), **kwargs
    )


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_guidance_kinds_are_the_trace_vocabulary() -> None:
    """`stages[].guidance_kind` is coerced to the same set the composer accepts."""
    assert GUIDANCE_KINDS is routing_trace.GUIDANCE_KINDS
    assert (GUIDANCE_HELP, GUIDANCE_NONE, GUIDANCE_CLARIFY) == ("help", "none", "clarify")
    assert GUIDANCE_KINDS == {"help", "none", "clarify"}


# ---------------------------------------------------------------------------
# Each reply shape, pinned whole
# ---------------------------------------------------------------------------


def test_help_over_the_senders_own_ballot() -> None:
    reply = _compose(
        GUIDANCE_HELP,
        _agent("Joker", "Tells jokes and light banter"),
        _identity("Anna Admin", "Ask Anna's assistants"),
    )

    assert reply == (
        "Here's who I can hand your message to:\n"
        "- **Joker** — Tells jokes and light banter\n"
        f"- **Anna Admin** — {IDENTITY_DESCRIPTION}\n"
        "Just describe what you need and I'll pick the right one."
    )


def test_an_identity_entry_never_shows_its_trigger_prompt_or_the_owners_address() -> None:
    """An identity's trigger prompt is platform-written for the classifier and
    carries the owner's email; an owner with no full name is named by email.
    Neither address may reach the sender — only the local part, and a fixed
    description."""
    email = "anna.admin@corp.example"
    reply = _compose(
        GUIDANCE_HELP,
        _identity(email, f"Contact Anna ({email}). Handles HR questions."),
        max_listed=5,
    )

    assert reply.splitlines()[1] == f"- **anna.admin** — {IDENTITY_DESCRIPTION}"
    assert email not in reply
    assert "corp.example" not in reply
    assert "Handles HR questions" not in reply


def test_the_plain_shape_email_receives() -> None:
    """The exact text/plain shape: no `**`, bullets and the "more" line kept."""
    guidance = build_guidance(
        GUIDANCE_HELP,
        [
            _agent("Joker", "tells jokes and light banter"),
            _identity("anna.admin@corp.example", "Contact anna.admin@corp.example"),
            _agent("A"),
            _agent("B"),
        ],
        max_listed=2,
    )

    assert compose_guidance_reply(guidance, markdown=False) == (
        "Here's who I can hand your message to:\n"
        "- Joker — tells jokes and light banter\n"
        f"- anna.admin — {IDENTITY_DESCRIPTION}\n"
        "…and 2 more.\n"
        "Just describe what you need and I'll pick the right one."
    )


def test_help_over_an_empty_ballot_offers_to_set_up_catalog_bundles() -> None:
    reply = _compose(GUIDANCE_HELP, _bundle("Support Desk", "Tickets and outages"))

    assert reply == (
        "I don't have an assistant set up for you here yet. I can set one up:\n"
        "- **Support Desk** — Tickets and outages\n"
        "Describe what you need and I'll set it up."
    )


def test_none_over_the_senders_own_ballot() -> None:
    reply = _compose(GUIDANCE_NONE, _agent("Joker", "Jokes"), _agent("HR Helper", "Leave"))

    assert reply == (
        "I couldn't find an assistant for that. I can hand your message to:\n"
        "- **Joker** — Jokes\n"
        "- **HR Helper** — Leave\n"
        "Rephrase your message, or name the one you mean."
    )


def test_none_over_an_empty_ballot_offers_catalog_bundles() -> None:
    reply = _compose(GUIDANCE_NONE, _bundle("Support Desk", "Tickets"))

    assert reply == (
        "I couldn't find an assistant for that. I can set one of these up for you:\n"
        "- **Support Desk** — Tickets\n"
        "Rephrase your message, or name the one you mean."
    )


def test_a_ballot_mixing_agents_and_bundles_is_not_read_as_a_setup_offer() -> None:
    """Only an all-bundle list says "set one up"; one real agent makes it a hand-off."""
    reply = _compose(GUIDANCE_HELP, _agent("Joker"), _bundle("Support Desk"))

    assert reply.startswith("Here's who I can hand your message to:\n"), reply


@pytest.mark.parametrize(
    "count,word",
    [(2, "two"), (3, "three"), (4, "4")],
)
def test_clarify_is_a_numbered_question_best_pick_first(count: int, word: str) -> None:
    entries = [_agent(f"Agent{i}", f"Does thing {i}") for i in range(1, count + 1)]

    reply = _compose(GUIDANCE_CLARIFY, *entries)

    assert reply == "\n".join(
        [
            f"That could go to {word} different assistants:",
            *(f"{i}. **Agent{i}** — Does thing {i}" for i in range(1, count + 1)),
            "Reply with the number or the name. Anything else and I'll treat it "
            "as a new request.",
        ]
    )


# ---------------------------------------------------------------------------
# Cap and "and N more"
# ---------------------------------------------------------------------------


def test_the_list_is_capped_and_the_rest_are_counted() -> None:
    entries = [_agent(f"A{i}") for i in range(7)]

    guidance = build_guidance(GUIDANCE_HELP, entries, max_listed=5)

    assert guidance is not None
    assert guidance.total == 7
    assert [e.name for e in guidance.entries] == ["A0", "A1", "A2", "A3", "A4"]
    assert guidance.ref_ids == tuple(f"agent-A{i}" for i in range(5))
    reply = compose_guidance_reply(guidance)
    assert reply.splitlines() == [
        "Here's who I can hand your message to:",
        *(f"- **A{i}**" for i in range(5)),
        "…and 2 more.",
        "Just describe what you need and I'll pick the right one.",
    ]


def test_a_list_exactly_at_the_cap_says_nothing_about_more() -> None:
    reply = _compose(GUIDANCE_NONE, *[_agent(f"A{i}") for i in range(5)])

    assert "more." not in reply
    assert reply.count("\n- ") == 5


@pytest.mark.parametrize("max_listed", [0, -3])
def test_a_cap_below_one_still_lists_one(max_listed: int) -> None:
    """A misconfigured cap must not produce an empty list under "and N more"."""
    guidance = build_guidance(
        GUIDANCE_HELP, [_agent("A"), _agent("B")], max_listed=max_listed
    )

    assert guidance is not None
    assert [e.name for e in guidance.entries] == ["A"]
    assert compose_guidance_reply(guidance).splitlines()[1:3] == ["- **A**", "…and 1 more."]


# ---------------------------------------------------------------------------
# Nothing to say
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["route", "", None, "HELP", "escalate"])
def test_build_guidance_refuses_a_kind_outside_the_vocabulary(kind) -> None:
    assert build_guidance(kind, [_agent("A")], max_listed=5) is None


@pytest.mark.parametrize("kind", sorted(GUIDANCE_KINDS))
def test_build_guidance_with_nothing_to_list_is_none(kind: str) -> None:
    """A list of nothing is `REPLY_NO_MATCH`, which the caller already sends."""
    assert build_guidance(kind, [], max_listed=5) is None
    assert build_guidance(kind, (), max_listed=5) is None


def test_compose_is_total_over_empty_and_unknown_guidance() -> None:
    assert compose_guidance_reply(None) is None
    assert compose_guidance_reply(RoutingGuidance(kind=GUIDANCE_HELP)) is None
    assert (
        compose_guidance_reply(RoutingGuidance(kind="route", entries=(_agent("A"),), total=1))
        is None
    )


# ---------------------------------------------------------------------------
# Owner-authored text: one line, clamped, inert
# ---------------------------------------------------------------------------


def _only_entry_line(entry: GuidanceEntry, **kwargs) -> str:
    reply = _compose(GUIDANCE_HELP, entry, **kwargs)
    lines = reply.splitlines()
    assert len(lines) == 3, reply
    return lines[1]


def test_a_multi_line_trigger_prompt_becomes_one_line() -> None:
    line = _only_entry_line(
        _agent("Ops", "Handle production incidents.\n\n- pages\n\t- outages   and more\r\n")
    )

    assert line == "- **Ops** — Handle production incidents. - pages - outages and more"


def test_a_long_description_is_clamped_at_a_word_boundary() -> None:
    description = " ".join(["incident"] * 60)  # ~540 chars

    line = _only_entry_line(_agent("Ops", description))
    shown = line.split(" — ", 1)[1]

    assert shown.endswith("…"), shown
    assert len(shown) <= MAX_DESCRIPTION_CHARS
    # A word boundary, not a word cut in half.
    assert shown[:-1].split(" ")[-1] == "incident", shown


def test_a_long_name_is_clamped() -> None:
    line = _only_entry_line(_agent("N" * 300))
    name = line[len("- **") : -len("**")]

    assert name.endswith("…"), name
    assert len(name) <= MAX_NAME_CHARS


def test_a_blank_name_and_a_missing_description_still_render_a_line() -> None:
    assert _only_entry_line(_agent("   \n ")) == "- **Unnamed assistant**"
    assert _only_entry_line(_agent("Joker", "   ")) == "- **Joker**"


def test_emphasis_and_code_markers_in_owner_text_are_stripped() -> None:
    """An unbalanced `**` in a trigger prompt would close the bold early."""
    line = _only_entry_line(_agent("**Jo`ker", "does `code` and **bold"))

    assert line == "- **Joker** — does code and bold"


def test_owner_text_cannot_render_a_link_or_a_chat_mention() -> None:
    """A name shaped like a markdown link and a description holding Chat's raw
    `<users/all>` mention syntax are both neutralised: `[`, `]` and `~` are
    stripped; `<`, `>` and `|` become look-alikes that render as themselves."""
    reply = _compose(
        GUIDANCE_HELP,
        _agent("[x](http://e)", "ping <users/all> or <http://evil.example|click> ~~gone~~"),
    )

    for forbidden in ("[", "]", "](", "<", ">", "|", "~"):
        assert forbidden not in reply, (forbidden, reply)
    assert "‹users/all›" in reply, reply
    assert "¦" in reply, reply


@pytest.mark.parametrize("value", [None, "", "   \n ", "****", 42])
def test_safe_display_name_never_answers_an_empty_name(value) -> None:
    """The thread nameplate formats this straight into ``**{name}**``, so an
    empty answer would put a bare ``****`` on the thread."""
    assert safe_display_name(value) == "Unnamed assistant"


def test_safe_display_name_neutralises_what_the_nameplate_would_render() -> None:
    assert safe_display_name("**Pay`roll**\nBot") == "Payroll Bot"
    assert safe_display_name("anna@example.com") == "anna"
    assert "<" not in safe_display_name("<users/all>")
    assert len(safe_display_name("N" * 300)) <= MAX_NAME_CHARS


def test_plain_mode_drops_the_bold_markers_and_keeps_the_words() -> None:
    """Email is sent without markdown (`supports_markdown=False`)."""
    entries = (_agent("Joker", "Jokes"), _agent("HR Helper", "Leave"))

    rich = _compose(GUIDANCE_NONE, *entries)
    plain = _compose(GUIDANCE_NONE, *entries, markdown=False)

    assert "**" in rich
    assert "**" not in plain
    assert plain == rich.replace("**", "")


def test_plain_mode_applies_to_a_clarify_question_too() -> None:
    plain = _compose(GUIDANCE_CLARIFY, _agent("A", "a"), _agent("B", "b"), markdown=False)

    assert "**" not in plain
    assert "1. A — a" in plain.splitlines()
