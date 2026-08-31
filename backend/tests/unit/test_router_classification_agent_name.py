"""
What the classifier is actually *told* about each candidate.

These tests used to assert that ``AppMCPRoutingService._ai_classify`` put a
``name`` and a ``prompt_examples`` key into the list of dicts it handed to
``AIFunctionsService.route_to_agent``. That contract passed for the entire life
of Bug 1: ``prompt_examples`` **was** in the payload dict, and the renderer one
layer below built its agent block from ``id`` / ``name`` / ``trigger_prompt``
and dropped it on the floor. A test that stops at the payload cannot see that,
and this file is the test that should have.

So they now assert against the **rendered prompt** — the last artefact before
the model, and the only place where "the classifier was told" is a fact rather
than an intention. The candidate objects are checked too, because a field
missing from the ballot can never reach the prompt; but the prompt assertion is
the one that would have failed.

Phase 5 of ``docs/plans/channels_identity_unification/`` deleted
``AppMCPRoutingService._ai_classify`` along with the rest of the
``AppAgentRoute`` family — App MCP no longer has a renderer of its own.
``AgentClassifier.classify`` / ``agent_classifier.render_prompt`` are now the
one renderer every routing surface (Channel Pass 1/2, App MCP Stage 1, Identity
Stage 2) shares, so the App-MCP-labelled tests below are rewritten against that
shared entry point, building plain ``Candidate`` objects instead of the deleted
``EffectiveRoute``. The property they guard — a candidate's name and
``prompt_examples`` reach the model — is unchanged; only the door it is tested
through moved, because there is only one door left.

No DB, no Docker, no LLM calls — the provider is mocked at classifier depth.
"""
import uuid
from unittest.mock import MagicMock, patch

_PROVIDER_TARGET = "app.services.routing.agent_classifier.get_provider_manager"


def _capture_prompt(fn) -> tuple[object, str]:
    """Run ``fn()`` with the provider mocked; return (result, rendered prompt)."""
    with patch(_PROVIDER_TARGET) as mock_pm:
        mock_pm.return_value.generate_content.return_value = MagicMock(
            text='{"agent_id": "NONE"}'
        )
        result = fn()
        assert mock_pm.return_value.generate_content.called, (
            "the classifier never reached the provider — this test proves "
            "nothing about the prompt unless it was actually rendered"
        )
        prompt = mock_pm.return_value.generate_content.call_args.args[0]
    return result, prompt


def _candidate_block(prompt: str) -> str:
    """Just the rendered candidate list.

    ``rsplit``, not ``split``: the static template has its own
    "## Available Agents" heading (it is where the input format is explained to
    the model, and it now mentions **Example messages** by name), so splitting
    on the first occurrence returns the template's prose and every "no examples
    were rendered" assertion below becomes a test of the template instead of a
    test of the renderer. Bounded at "## User Message" for the same reason.
    """
    tail = prompt.rsplit("## Available Agents", 1)[1]
    return tail.split("## User Message", 1)[0]


def _candidate(**overrides):
    from app.services.routing.agent_classifier import Candidate

    base = dict(
        ref_id=str(uuid.uuid4()),
        name="Calendar Planner",
        trigger_prompt="Schedule meetings and manage calendar events",
        prompt_examples=None,
    )
    base.update(overrides)
    return Candidate(**base)


# ---------------------------------------------------------------------------
# The candidate's name reaches the prompt
# ---------------------------------------------------------------------------


def test_agent_name_reaches_the_rendered_prompt() -> None:
    """The agent's *name* is classification context, not decoration.

    It is what lets the model separate "Calendar Planner" from "Vacation
    Planner" when their trigger words overlap.
    """
    from app.services.routing.agent_classifier import AgentClassifier

    candidate = _candidate(name="Calendar Planner")
    _, prompt = _capture_prompt(
        lambda: AgentClassifier.classify(
            [candidate], "Can you schedule a meeting for tomorrow?"
        )
    )

    assert "Calendar Planner" in prompt
    assert candidate.ref_id in prompt
    assert "Schedule meetings and manage calendar events" in prompt


# ---------------------------------------------------------------------------
# Bug 1 — prompt_examples reaches the prompt
# ---------------------------------------------------------------------------


def test_prompt_examples_reach_the_rendered_prompt() -> None:
    """Bug 1's regression guard, asserted where the bug actually lived.

    ``prompt_examples`` is validated, stored, and documented to the agent's
    owner as a routing aid. Before Phase 5 it travelled all the way to the
    renderer and was discarded there, so the owner's examples changed nothing
    at all.
    """
    from app.services.routing.agent_classifier import AgentClassifier

    candidate = _candidate(prompt_examples="book a meeting\nschedule event")
    _, prompt = _capture_prompt(
        lambda: AgentClassifier.classify(
            [candidate], "Can you schedule a meeting for tomorrow?"
        )
    )

    assert "book a meeting" in prompt
    assert "schedule event" in prompt


def test_identity_bindings_send_their_prompt_examples_too() -> None:
    """The identity path never even *collected* the field before Phase 5.

    ``IdentityAgentBinding.prompt_examples`` is edited on the same screen as
    the App MCP one and was validated identically — it simply was not read when
    Stage 2 built its candidate dicts. Sharing one builder is what fixes that
    for good; this pins it.
    """
    from app.services.identity.identity_routing_service import IdentityRoutingService

    agent_id = uuid.uuid4()
    binding = MagicMock()
    binding.agent_id = agent_id
    binding.trigger_prompt = "Handles John's calendar"
    binding.prompt_examples = "when is john free\nbook time with john"

    db = MagicMock()
    agent = MagicMock()
    agent.name = "John's Calendar"
    db.get.return_value = agent

    _, prompt = _capture_prompt(
        lambda: IdentityRoutingService._ai_classify(
            "when is john free on friday", [binding, binding], db
        )
    )

    assert "when is john free" in prompt
    assert "book time with john" in prompt
    assert "John's Calendar" in prompt


def test_absent_prompt_examples_render_no_examples_block() -> None:
    """No examples must not render an empty, model-confusing heading."""
    from app.services.routing.agent_classifier import AgentClassifier

    candidate = _candidate(prompt_examples=None)
    _, prompt = _capture_prompt(
        lambda: AgentClassifier.classify([candidate], "process this invoice")
    )

    candidate_block = _candidate_block(prompt)
    assert "**Example messages**" not in candidate_block


def test_blank_prompt_examples_render_no_examples_block() -> None:
    """Whitespace-only examples are "no examples", not one blank bullet."""
    from app.services.routing.agent_classifier import AgentClassifier

    candidate = _candidate(prompt_examples="   \n\n  \n")
    _, prompt = _capture_prompt(
        lambda: AgentClassifier.classify([candidate], "process this invoice")
    )

    candidate_block = _candidate_block(prompt)
    assert "**Example messages**" not in candidate_block
