"""`RoutingReachabilityService` — the properties that need no database.

The verdict *sentences* are covered end-to-end, one test per branch, in
`tests/api/routing/routing_reachability_verdict_test.py` — they are statements
about what routing actually did and are proved against real routing decisions.
What is left here is the part of this service that is pure: totality, the
near-miss ranking, and the candidate roll-up.

Three properties, each one there because its failure mode is silent:

1. **`diagnose` never raises.** Its caller is `RoutingTraceService.get`, which
   serves the whole trace. A diagnosis that propagated would take the trace
   detail down with it — §11a Rule 2's shape, on a read path: the debugging aid
   breaking the thing it observes.
2. **The Jaccard helpers are *called*, not copied.** Plan §3 says reuse
   `AppAgentRouteService._jaccard_similarity` / `._tokens_for_similarity`
   verbatim. A copy would pass every behavioural test in this file and then
   drift the first time either side was tuned, so the test asserts the call
   itself rather than the numbers it produces.
3. **A candidate seen in two stages is one candidate.** "This user has N
   effective routes" is the verdict's headline number; counted twice it is a
   wrong number stated with confidence.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.models.routing.routing_decision import RoutingDecisionPublic
from app.services.app_mcp.app_agent_route_service import AppAgentRouteService
from app.services.routing import routing_trace
from app.services.routing.routing_reachability_service import (
    NEAR_MISS_LIMIT,
    RoutingReachabilityService,
)

_JACCARD = (
    "app.services.app_mcp.app_agent_route_service."
    "AppAgentRouteService._jaccard_similarity"
)


def _candidate(
    ref_id: str,
    name: str,
    *,
    trigger_prompt: str = "handle things",
    prompt_examples: str | None = None,
    eligible: bool = True,
    skip_reason: str | None = None,
) -> dict:
    return {
        "kind": routing_trace.KIND_AGENT,
        "ref_id": ref_id,
        "name": name,
        "trigger_prompt": trigger_prompt,
        "prompt_examples": prompt_examples,
        "eligible": eligible,
        "skip_reason": skip_reason,
    }


def _trace(stages: list, *, message: str | None = "handle things") -> RoutingDecisionPublic:
    return RoutingDecisionPublic(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        origin=routing_trace.ORIGIN_SIMULATE,
        outcome=routing_trace.OUTCOME_NO_MATCH,
        message_text=message,
        stages=stages,
    )


# ── 1. Totality ──────────────────────────────────────────────────────


class _Poison:
    """An object whose truthiness raises — one of the five shapes §11a Rule 2
    names, and the one `stages` readers meet first (`stage or {}`)."""

    def __bool__(self) -> bool:
        raise RuntimeError("poisoned __bool__")


def test_diagnose_reports_its_own_failure_instead_of_raising() -> None:
    """A stage payload that cannot be read must cost the diagnosis, not the trace.

    Fired with a poison object rather than proved by reading the code — that is
    the standard §11a Rule 2 sets, and it is how the two escapes in `clamp()`
    were found.
    """
    diagnosis = RoutingReachabilityService.diagnose(
        MagicMock(), _trace([_Poison()])
    )

    assert diagnosis.code == "unavailable"
    assert diagnosis.verdict == (
        "This decision's diagnosis could not be computed, but the trace itself "
        "is intact. Read the candidate list below, and see the server logs for "
        "why the summary failed."
    )
    # Even the failure branch names what to do next.
    assert diagnosis.action in diagnosis.verdict


# ── 2. The Jaccard helpers are called, not reimplemented ─────────────


def test_near_miss_ranking_calls_the_shared_jaccard_helper() -> None:
    """Patching `AppAgentRouteService._jaccard_similarity` must change the
    ranking. If this module ever grows its own copy of the overlap formula the
    patch stops reaching it and this goes red — which is the entire point,
    because a copy is otherwise invisible until the two disagree.
    """
    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [
                    _candidate("a", "Alpha", trigger_prompt="alpha things"),
                    _candidate("b", "Beta", trigger_prompt="beta things"),
                ],
            }
        ]
    )

    with patch(_JACCARD, return_value=0.42) as jaccard:
        diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)

    assert jaccard.call_count == 2, jaccard.call_args_list
    assert [m.similarity for m in diagnosis.near_misses] == [0.42, 0.42]


def test_near_miss_ranking_scores_prompt_examples_too() -> None:
    """The ranking must score what the classifier *sees*, not a subset of it.

    Phase 5 made `prompt_examples` part of the rendered prompt (Bug 1). If the
    ranking kept scoring `trigger_prompt` alone, an agent with a vague trigger
    and exact examples would win the route while the tuning card ranked it last
    — the feature misreporting the system on its own diagnostic surface.

    Asserted against real Jaccard output rather than a patched score, because
    the property is *which text is fed in*, and a patched helper would happily
    return the same number either way.
    """
    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [
                    _candidate(
                        "vague",
                        "Ops Runbook",
                        trigger_prompt="Internal operations helper",
                        prompt_examples="restart the payment worker",
                    ),
                    _candidate(
                        "plain",
                        "Trip Planner",
                        trigger_prompt="Internal operations helper",
                    ),
                ],
            }
        ],
        message="restart the payment worker",
    )

    diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)
    scores = {m.name: m.similarity for m in diagnosis.near_misses}

    # Identical trigger prompts; the examples are the only difference, so any
    # gap between them is the examples being scored.
    assert scores["Ops Runbook"] > scores["Trip Planner"], scores
    assert diagnosis.near_miss_notice is None


def test_a_candidate_with_only_prompt_examples_is_still_ranked() -> None:
    """No trigger prompt used to mean "unrankable"; examples are text too.

    Skipping it would drop exactly the candidate whose owner leaned on examples
    instead of prose — and drop it silently, since the card renders a short list.
    """
    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [
                    _candidate(
                        "examples-only",
                        "Examples Only",
                        trigger_prompt="",
                        prompt_examples="restart the payment worker",
                    )
                ],
            }
        ],
        message="restart the payment worker",
    )

    diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)

    assert [m.name for m in diagnosis.near_misses] == ["Examples Only"]
    assert diagnosis.near_misses[0].similarity > 0


def test_near_miss_ranking_is_ordered_and_capped() -> None:
    """Best first, and short: the tail of a Jaccard ranking is stopword noise."""
    candidates = [
        _candidate(f"r{i}", f"Agent{i}", trigger_prompt=f"prompt{i}")
        for i in range(NEAR_MISS_LIMIT + 3)
    ]
    trace = _trace(
        [{"stage": routing_trace.STAGE_PASS_1, "candidates": candidates}]
    )
    # Descending scores handed back in ascending candidate order, so a service
    # that returned them unsorted would produce exactly the reverse.
    scores = iter([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2])

    with patch(_JACCARD, side_effect=lambda *_: next(scores)):
        diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)

    assert len(diagnosis.near_misses) == NEAR_MISS_LIMIT
    assert [m.name for m in diagnosis.near_misses] == [
        f"Agent{i}" for i in range(NEAR_MISS_LIMIT)
    ]
    assert [m.similarity for m in diagnosis.near_misses] == [0.9, 0.8, 0.7, 0.6, 0.5]


def test_the_shared_tokenizer_is_the_one_being_used() -> None:
    """The tokenizer, not only the overlap function.

    Asserted by agreeing with `AppAgentRouteService` on a case the naive
    alternative gets wrong: its tokenizer drops tokens shorter than three
    characters, so "my ok" and "ok my" have *no* tokens at all and a plain
    word-set overlap would say 1.0 where this says 0.0.
    """
    assert AppAgentRouteService._tokens_for_similarity("my ok") == set()

    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [_candidate("a", "Alpha", trigger_prompt="ok my")],
            }
        ],
        message="my ok",
    )
    diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)

    assert [m.similarity for m in diagnosis.near_misses] == [0.0]


# ── 3. Candidate roll-up ─────────────────────────────────────────────


def test_a_candidate_seen_in_two_stages_is_counted_once() -> None:
    """`mark_candidate_skipped` flips a row in place, so the same `ref_id` can
    legitimately appear in more than one stage. The later occurrence wins — it
    carries the more settled verdict — and the count stays at one.
    """
    shared = "same-agent"
    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [_candidate(shared, "Shared")],
            },
            {
                "stage": routing_trace.STAGE_PASS_2,
                "candidates": [
                    _candidate(
                        shared,
                        "Shared",
                        eligible=False,
                        skip_reason=routing_trace.SKIP_FOREIGN_OWNER,
                    )
                ],
            },
        ]
    )

    diagnosis = RoutingReachabilityService.diagnose(MagicMock(), trace)

    assert diagnosis.eligible_candidate_count == 0
    assert diagnosis.skipped_by_reason == {routing_trace.SKIP_FOREIGN_OWNER: 1}
    assert len(diagnosis.near_misses) == 1


def test_an_unmapped_skip_reason_is_reported_by_name_rather_than_guessed() -> None:
    """The explanation table is deliberately incomplete-safe.

    A `skip_reason` this build has never heard of — the shape every string
    vocabulary in this feature can produce — must degrade to an honest "no
    explanation for this" naming the raw value, never to a confident sentence
    about the wrong cause.
    """
    ref = str(uuid.uuid4())
    trace = _trace(
        [
            {
                "stage": routing_trace.STAGE_PASS_1,
                "candidates": [
                    _candidate(
                        ref, "Mystery", eligible=False, skip_reason="invented_later"
                    )
                ],
            }
        ]
    )
    db = MagicMock()
    db.get.return_value = None  # no agent row; the trace answers this one

    diagnosis = RoutingReachabilityService.diagnose(
        db, trace, expected_agent_id=uuid.UUID(ref)
    )

    assert diagnosis.code == "expected_agent_skipped"
    assert diagnosis.verdict == (
        "Mystery was considered for this decision and then excluded: it was "
        "excluded with reason 'invented_later', which this build has no "
        "explanation for. Read the candidate row below and the router's logs — "
        "this reason was added after the diagnosis was written."
    )
