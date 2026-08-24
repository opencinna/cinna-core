"""
`AgentClassifier`'s parse of the Phase 5 prompt contract.

The contract grew three advisory fields — `confidence`, `reason`, `runner_up` —
and the property that matters most about them is **negative**: a model that
ignores all three must still route. Local and small models routinely drop
fields added to a JSON schema, and this project's own default cascade ends at a
self-hosted `granite4`, so a strict parse would have turned a tuning feature
into a routing outage for exactly the deployments least able to diagnose it.

Everything here is a regression guard (it asserts behaviour the parse could
plausibly break), not a precondition assertion — each one is expected to survive
a mutation check.

No DB, no Docker, no LLM calls.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.routing import routing_trace
from app.services.routing.agent_classifier import (
    AgentClassifier,
    Candidate,
    _parse_confidence,
    _parse_reason,
    _parse_runner_up,
)

_PROVIDER_TARGET = "app.services.routing.agent_classifier.get_provider_manager"

AGENT_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
CANDIDATES = [
    Candidate(ref_id=AGENT_ID, name="Ops Runbook", trigger_prompt="Ops things"),
    Candidate(ref_id=OTHER_ID, name="People Ops", trigger_prompt="Policy things"),
]


def _classify(reply: str, message: str = "prod is down"):
    with patch(_PROVIDER_TARGET) as mock_pm:
        mock_pm.return_value.generate_content.return_value = MagicMock(text=reply)
        return AgentClassifier.classify(CANDIDATES, message)


# ---------------------------------------------------------------------------
# The one that keeps a tuning feature from becoming an outage
# ---------------------------------------------------------------------------


def test_a_reply_omitting_every_new_field_still_routes() -> None:
    """The pre-Phase-5 reply shape, which every deployed model still emits.

    If this ever fails, small-model deployments stop routing entirely — the
    field additions are advisory and must never be load-bearing.
    """
    result = _classify(json.dumps({"agent_id": AGENT_ID, "message": None}))

    assert result is not None
    assert result.agent_id == AGENT_ID
    assert result.confidence is None
    assert result.reason is None
    assert result.runner_up_id is None


def test_a_reply_with_garbage_in_every_new_field_still_routes() -> None:
    """Wrong *types*, not just missing keys — the other half of the same risk."""
    result = _classify(
        json.dumps(
            {
                "agent_id": AGENT_ID,
                "message": None,
                "confidence": "very sure",
                "reason": {"nested": "object"},
                "runner_up": ["a", "list"],
            }
        )
    )

    assert result is not None
    assert result.agent_id == AGENT_ID
    assert result.confidence is None
    assert result.reason is None
    assert result.runner_up_id is None


def test_a_json_reply_that_is_not_an_object_is_a_no_match_not_a_crash() -> None:
    assert _classify("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# What the fields carry when the model does answer
# ---------------------------------------------------------------------------


def test_the_advisory_fields_are_carried_through_when_present() -> None:
    result = _classify(
        json.dumps(
            {
                "agent_id": AGENT_ID,
                "message": None,
                "confidence": 0.72,
                "reason": "it names a production outage",
                "runner_up": OTHER_ID,
            }
        )
    )

    assert result is not None
    assert result.confidence == 0.72
    assert result.reason == "it names a production outage"
    assert result.runner_up_id == OTHER_ID


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.0, 0.0),
        (1.0, 1.0),
        (0.5, 0.5),
        ("0.5", 0.5),
        (1, 1.0),
        # Out of range is DROPPED, never rescaled: an `85` probably means 85%,
        # and "probably" is how a diagnostic starts reporting numbers the model
        # never gave it.
        (85, None),
        (-0.1, None),
        (1.01, None),
        (float("nan"), None),
        (float("inf"), None),
        # `True` is an `int` in Python and would otherwise arrive as 1.0.
        (True, None),
        (None, None),
        ("high", None),
        ([0.5], None),
    ],
)
def test_confidence_parsing(raw, expected) -> None:
    # Plain equality, not a disjunction: an `or`-ed fallback in an assertion is
    # a second way for the test to pass, which is one more than it should have.
    assert _parse_confidence(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("because it is an ops question", "because it is an ops question"),
        ("  padded  ", "padded"),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
        ({"a": 1}, None),
    ],
)
def test_reason_parsing(raw, expected) -> None:
    assert _parse_reason(raw) == expected


def test_runner_up_must_name_a_candidate_on_the_ballot() -> None:
    """An allowlist, not a UUID format check.

    A runner-up naming something that was never a candidate is not a diagnosis;
    it is a dangling reference the tuning card would render as a real one.
    """
    known = {AGENT_ID, OTHER_ID}
    assert _parse_runner_up(OTHER_ID, known) == OTHER_ID
    assert _parse_runner_up(str(uuid.uuid4()), known) is None
    assert _parse_runner_up("NONE", known) is None
    assert _parse_runner_up("none", known) is None
    assert _parse_runner_up("", known) is None
    assert _parse_runner_up(None, known) is None
    assert _parse_runner_up(12345, known) is None


# ---------------------------------------------------------------------------
# What reaches the trace
# ---------------------------------------------------------------------------


def test_confidence_reaches_both_the_stage_and_the_decision() -> None:
    """The `confidence` column exists to be filled by exactly this path.

    Without the decision-level lift the column would be permanently NULL while
    reading, to an operator, as "the model gave no confidence" — a hazardous
    state wearing an ordinary one's clothes (plan §11a, Rule 1).
    """
    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SIMULATE,
        stage=routing_trace.STAGE_PASS_1,
        message="prod is down",
    ) as trace:
        _classify(
            json.dumps({"agent_id": AGENT_ID, "confidence": 0.64, "reason": "ops"})
        )
        routing_trace.record_outcome(
            routing_trace.OUTCOME_ROUTED, selected_agent_id=AGENT_ID
        )
        stages = trace.stages_payload()
        decision_confidence = trace.confidence

    assert stages[0]["confidence"] == 0.64
    assert stages[0]["reason"] == "ops"
    assert decision_confidence == 0.64


def test_a_non_routed_outcome_clears_the_lifted_confidence() -> None:
    """A `no_match` row must not carry a score for an agent it does not name.

    The classifier picked something and a downstream filter threw it out; the
    settler clears the selection, and the confidence has to go with it or the
    row reports a decision that was not taken.
    """
    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SIMULATE,
        stage=routing_trace.STAGE_PASS_1,
        message="prod is down",
    ) as trace:
        _classify(json.dumps({"agent_id": AGENT_ID, "confidence": 0.64}))
        routing_trace.record_outcome(routing_trace.OUTCOME_NO_MATCH)
        decision_confidence = trace.confidence

    assert decision_confidence is None


def test_reason_is_not_on_the_message_text_allowlist() -> None:
    """Pinned here as well as end-to-end, because this is the *rule*.

    The API test proves the withholding happens; this one pins why it can, and
    fails the moment somebody puts the field back to make a stage read nicer.
    """
    assert "reason" not in routing_trace.SAFE_STAGE_FIELDS
    # ...while the two neighbours that are genuinely not sender-derived stay.
    assert "confidence" in routing_trace.SAFE_STAGE_FIELDS
    assert "runner_up_id" in routing_trace.SAFE_STAGE_FIELDS
