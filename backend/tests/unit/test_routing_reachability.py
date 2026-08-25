"""`RoutingReachabilityService` — the properties that need no database.

The verdict *sentences* are covered end-to-end, one test per branch, in
`tests/api/routing/routing_reachability_verdict_test.py` — they are statements
about what routing actually did and are proved against real routing decisions.
What is left here is the part of this service that is pure: totality, the
near-miss ranking, and the candidate roll-up.

Four properties, each one there because its failure mode is silent:

1. **`diagnose` never raises.** Its caller is `RoutingTraceService.get`, which
   serves the whole trace. A diagnosis that propagated would take the trace
   detail down with it — §11a Rule 2's shape, on a read path: the debugging aid
   breaking the thing it observes.
2. **The Jaccard helpers are *called*, not copied.** Plan §3 says reuse
   `app.services.routing.text_similarity`'s `jaccard_similarity` /
   `tokens_for_similarity` verbatim. A copy would pass every behavioural test in
   this file and then drift the first time either side was tuned, so the test
   asserts the call itself rather than the numbers it produces.
3. **A candidate seen in two stages is one candidate.** "This user has N
   effective routes" is the verdict's headline number; counted twice it is a
   wrong number stated with confidence.
4. **The verdict is split by `origin`**, and some of the split cannot be driven
   through a real decision at all — which is why the last section is here rather
   than in the API file next to the rest of the sentences.

   Three shapes are undrivable, for three different reasons, and the last one is
   the interesting case:

   - An `origin="app_mcp"` decision **with a candidate list**. Nothing opens an
     App MCP capture (`ORIGIN_APP_MCP` is reserved vocabulary; routing_tuning
     Phase 6 owns emitting it), and the seeded-row exemption the API file uses
     carries no candidates. So the App MCP *count noun* — plan §9's "3 effective
     routes" headline — has nowhere else to be asserted.
   - Skip reasons **no surface produces any more**: `identity_route` (deleted
     from the channel path by the scope split) and `foreign_owner` (kept as a
     defence-in-depth postcondition that is unreachable by construction, since
     every channel candidate came out of `WHERE owner_id = sender`). Rows
     written before the split still carry them and the admin UI still renders
     them, so their sentences still have to be right.
   - Skip reasons a channel can produce only through a **race** —
     `agent_missing` needs the winning agent deleted between the candidate scan
     and the lookup — plus `route_inactive`, whose producer moved off the
     channel path entirely but whose historical rows did not.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.models import Agent
from app.models.routing.routing_decision import RoutingDecisionPublic
from app.services.routing import routing_trace
from app.services.routing.routing_reachability_service import (
    NEAR_MISS_LIMIT,
    RoutingReachabilityService,
)
from app.services.routing.text_similarity import tokens_for_similarity

# Patched at its *definition site* on purpose. `routing_reachability_service`
# reaches these through the module (`text_similarity.jaccard_similarity(...)`)
# rather than binding the name locally, so this target is only reachable if the
# shared implementation really is the one running. A local copy — or a
# `from ... import jaccard_similarity` rebind — would leave this patch unused
# and the assertions below red.
_JACCARD = "app.services.routing.text_similarity.jaccard_similarity"


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


def _trace(
    stages: list,
    *,
    message: str | None = "handle things",
    origin: str = routing_trace.ORIGIN_SIMULATE,
    user_id: uuid.UUID | None = None,
) -> RoutingDecisionPublic:
    """A projected decision. Defaults to a **channel** origin.

    `ORIGIN_SIMULATE` is a channel origin — simulate and replay both wrap
    `ChannelRoutingService.decide` — so the default here is the live surface,
    and a test wanting the App MCP half says so explicitly.
    """
    return RoutingDecisionPublic(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        origin=origin,
        outcome=routing_trace.OUTCOME_NO_MATCH,
        message_text=message,
        stages=stages,
        user_id=user_id,
    )


class _DB:
    """The two `db.get` lookups `_expected_agent_verdict` makes, and no more.

    A `MagicMock` cannot stand in here: `_agent_label` reads `agent.name` and
    would get a mock whose `.strip()` is truthy, so the verdict would name a
    repr instead of an agent. Keyed on the model class rather than the id
    because that is the only distinction the service draws.
    """

    def __init__(self, *, agent=None, owner=None) -> None:
        self._agent = agent
        self._owner = owner

    def get(self, model, ident):  # noqa: ANN001 — mirrors Session.get
        return self._agent if model is Agent else self._owner


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
    """Patching `text_similarity.jaccard_similarity` must change the ranking.

    If this module ever grows its own copy of the overlap formula the patch
    stops reaching it and this goes red — which is the entire point, because a
    copy is otherwise invisible until the two disagree.
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

    Asserted by agreeing with `text_similarity` on a case the naive alternative
    gets wrong: its tokenizer drops tokens shorter than three characters, so
    "my ok" and "ok my" have *no* tokens at all and a plain word-set overlap
    would say 1.0 where this says 0.0.
    """
    assert tokens_for_similarity("my ok") == set()

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


# ── 4. The origin split, where a real decision cannot reach it ───────


def _skipped(reason: str, *, kind: str = routing_trace.KIND_AGENT) -> tuple[str, dict]:
    """One skipped candidate and its ref_id, ready to hand to `_trace`."""
    ref = str(uuid.uuid4())
    row = _candidate(ref, "Expected Agent", eligible=False, skip_reason=reason)
    row["kind"] = kind
    return ref, {"stage": routing_trace.STAGE_PASS_1, "candidates": [row]}


def _skip_verdict(reason: str, *, origin: str, kind: str = routing_trace.KIND_AGENT):
    ref, stage = _skipped(reason, kind=kind)
    return RoutingReachabilityService.diagnose(
        _DB(),  # no agent row: the trace answers this one
        _trace([stage], origin=origin),
        expected_agent_id=uuid.UUID(ref),
    )


def test_an_already_installed_bundle_is_not_told_to_check_an_app_mcp_route() -> None:
    """The last live instance of the §2.4 defect, and the reason the split is by
    origin rather than by candidate kind.

    `already_installed` is recorded by Pass 2's auto-install scan as a
    `KIND_BUNDLE` candidate, Pass 2 runs on channel decisions, and its remedy
    used to read "Check the installed agent's App MCP route". A `kind == agent`
    gate would have left exactly this one pointing at the wrong control — so the
    kind is deliberately the *bundle* one here.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_ALREADY_INSTALLED,
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        kind=routing_trace.KIND_BUNDLE,
    )

    assert diagnosis.code == "expected_agent_skipped"
    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: "
        "this user already has it installed, so the auto-install pass passed "
        "over it — it should have been reachable in Pass 1 as one of the "
        "agents they own instead. Set a router trigger prompt (or example "
        "prompts) on the installed agent's Configuration tab: an install with "
        "neither is not a channel candidate, which is exactly this gap."
    )
    assert "App MCP" not in diagnosis.verdict


def test_an_already_installed_bundle_keeps_the_app_mcp_wording_on_app_mcp() -> None:
    """The same finding, the other surface. There the route genuinely is the
    thing to check, so the original sentence stands unchanged."""
    diagnosis = _skip_verdict(
        routing_trace.SKIP_ALREADY_INSTALLED,
        origin=routing_trace.ORIGIN_APP_MCP,
        kind=routing_trace.KIND_BUNDLE,
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: "
        "this user already has it installed, so the auto-install pass passed "
        "over it — it should have been reachable as one of their own routes "
        "instead. Check the installed agent's App MCP route: an install whose "
        "route is missing or switched off falls into exactly this gap."
    )


def test_a_dangling_agent_id_on_a_channel_is_not_blamed_on_a_route() -> None:
    """`agent_missing` on a channel has no route in its story at all.

    Its producer moved: channel Pass 1 records it when the winning candidate's
    row is gone by the time it is loaded, and that candidate came out of
    `WHERE owner_id = sender`. Telling the reader to delete a dangling route
    would send them looking for a row that was never involved.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_AGENT_MISSING, origin=routing_trace.ORIGIN_SERVER_CHANNEL
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: "
        "the candidate that won names an agent id with no agent behind it — it "
        "was deleted between this decision's candidate scan and the lookup "
        "that follows it. Re-run this decision. If the agent is meant to "
        "exist, recreate it and set a router trigger prompt (or example "
        "prompts) on its Configuration tab."
    )


def test_an_inactive_route_on_a_channel_explains_history_but_not_a_remedy() -> None:
    """The clearest case of the rule the split is drawn along.

    `route_inactive` has no channel producer any more, but channel traces
    captured before the scope split carry it and are still read. The
    **explanation** keeps describing what happened — history does not change
    with the surface reading it — while the **action** cannot, because
    switching that route back on would not make the agent a channel candidate
    today. An explanation clause may look backwards; an action clause never
    does.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_ROUTE_INACTIVE, origin=routing_trace.ORIGIN_SERVER_CHANNEL
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: "
        "its App MCP route was switched off when this decision ran, and this "
        "trace was captured while channel routing still read App MCP routes. "
        "Set a router trigger prompt (or example prompts) on the agent's "
        "Configuration tab — switching that route back on would not help, "
        "because channel routing no longer reads routes at all."
    )


def test_a_bundle_with_no_trigger_prompt_is_not_told_about_example_prompts() -> None:
    """The one place `kind` is consulted, and why it has to be.

    `no_trigger_prompt` has two producers a single channel decision reaches:
    the candidate provider records it for an **agent** with neither field, and
    Pass 2's scan records it for a **bundle** revision that carried no prompt.
    A bundle revision has no `example_prompts` of its own, so the agent wording
    would prescribe a field that is not there.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_NO_TRIGGER_PROMPT,
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        kind=routing_trace.KIND_BUNDLE,
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: it "
        "has no router trigger prompt, so the classifier had nothing to match "
        "the message against. Set a router trigger prompt on the agent's "
        "Configuration tab (for a bundle, on the revision that gets published)."
    )
    assert "example prompts" not in diagnosis.verdict


def test_the_identity_route_skips_base_entry_still_explains_itself() -> None:
    """No surface produces `identity_route` any more — the scope split deleted
    the branch that recorded it — but rows written before it still render in the
    admin UI, so both table entries have to be right.

    This is the **base** entry, reached on a non-channel origin. It keeps the
    original remedy; the channel override that replaces it is pinned below.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_IDENTITY_ROUTE, origin=routing_trace.ORIGIN_APP_MCP
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: it "
        "was reached through an identity contact route, which hands off to that "
        "person's agents in a second stage and is not selectable from a "
        "channel. Route to the contact rather than to their agent, or give this "
        "user their own install of it."
    )


def test_the_foreign_owner_skip_still_explains_itself() -> None:
    """`foreign_owner` is now a postcondition that is unreachable by
    construction — every channel candidate came out of
    `WHERE owner_id = sender` — and is kept deliberately, as is its
    explanation, for the rows that predate the split."""
    diagnosis = _skip_verdict(
        routing_trace.SKIP_FOREIGN_OWNER, origin=routing_trace.ORIGIN_SERVER_CHANNEL
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: it "
        "belongs to a different account, and a channel session must run on the "
        "sender's own install. Share the agent's bundle with this user and have "
        "them install it — routing to somebody else's install is refused by "
        "design."
    )


def test_the_counted_noun_follows_the_origin() -> None:
    """Plan §9's headline number, in each surface's own vocabulary.

    "3 effective routes" on a channel is wrong twice over: nothing on that
    ballot is a route, and the phrase sends an admin to a routes list to look
    for three rows that need not exist. The App MCP half is the sentence §9
    actually specifies, and this is the only place it can be asserted with a
    candidate list behind it — nothing opens an App MCP capture.
    """
    stage = {
        "stage": routing_trace.STAGE_PASS_1,
        "candidates": [_candidate(f"r{i}", f"Agent{i}") for i in range(3)],
    }

    channel = RoutingReachabilityService.diagnose(
        _DB(), _trace([stage], origin=routing_trace.ORIGIN_SERVER_CHANNEL)
    )
    app_mcp = RoutingReachabilityService.diagnose(
        _DB(), _trace([stage], origin=routing_trace.ORIGIN_APP_MCP)
    )

    assert channel.code == app_mcp.code == "no_match"
    assert channel.verdict.startswith(
        "This user has 3 eligible candidates and the classifier matched none of "
        "them."
    )
    assert app_mcp.verdict.startswith(
        "This user has 3 effective routes and the classifier matched none of "
        "them."
    )


def test_an_unknown_origin_gets_the_app_mcp_wording() -> None:
    """The default has to land somewhere, and it lands on the wording every
    origin had before the split.

    Safe in both directions: an unknown origin is no worse off than it was, and
    the two reserved origins a future build will actually emit (`app_mcp`,
    `identity`) are precisely the surfaces that do require a route. A channel
    silently diagnosed as App MCP is the §2.4 defect; App MCP diagnosed as a
    channel would be its mirror image, and only the known channel origins are
    ever treated as one.
    """
    stage = {
        "stage": routing_trace.STAGE_PASS_1,
        "candidates": [_candidate("r1", "Agent1")],
    }
    diagnosis = RoutingReachabilityService.diagnose(
        _DB(), _trace([stage], origin="something_added_later")
    )

    assert diagnosis.verdict.startswith("This user has 1 effective route")


def test_a_channel_verdict_never_claims_an_owner_when_the_sender_is_gone() -> None:
    """`RoutingDecision.user_id` is `SET NULL`, deliberately — a trace still
    explains a routing rule after the account is gone.

    A channel candidate is defined *entirely* by ownership, so with no sender
    there is nothing to check the agent's owner against. The branch that
    otherwise catches this one says "this user owns it", which about no user at
    all is exactly the confidently-wrong diagnosis this module exists to avoid.
    """
    agent = Agent(
        name="Orphaned Trace Agent",
        owner_id=uuid.uuid4(),
        router_trigger_prompt="handle things",
    )
    diagnosis = RoutingReachabilityService.diagnose(
        _DB(agent=agent),
        _trace(
            [{"stage": routing_trace.STAGE_PASS_1, "candidates": []}],
            origin=routing_trace.ORIGIN_SERVER_CHANNEL,
            user_id=None,
        ),
        expected_agent_id=agent.id,
    )

    assert diagnosis.code == "expected_agent_sender_gone"
    assert diagnosis.verdict == (
        "This user has 0 eligible candidates; Orphaned Trace Agent is not "
        "among them because this decision's sender account no longer exists, "
        "and a channel candidate is defined entirely by who owns it — with no "
        "sender there is nothing left to check its owner against. Run Simulate "
        "for the account you actually mean; this trace can no longer answer a "
        "question about ownership."
    )
    # The specific claim the fall-through would have made.
    assert "this user owns it" not in diagnosis.verdict


def test_the_channel_eligibility_test_accepts_example_prompts_alone() -> None:
    """`_has_router_wording` calls `ChannelCandidateProvider`'s own example
    reader rather than restating it.

    Three shapes in one test because they are one rule: a blank-only list is
    not examples (so `[""]` must not make an agent look reachable), a real list
    is, and a non-list column — the JSON field has no write-time validator —
    must degrade to "no examples" instead of iterating a string into
    characters. A second copy of this rule would disagree with the provider
    here first, and would do it silently.
    """
    from app.services.routing.routing_reachability_service import _has_router_wording

    def _agent(**fields) -> Agent:
        return Agent(name="Wordless", owner_id=uuid.uuid4(), **fields)

    assert not _has_router_wording(_agent(example_prompts=[]))
    assert not _has_router_wording(_agent(example_prompts=["", "  "]))
    assert not _has_router_wording(_agent(example_prompts="not a list"))
    assert _has_router_wording(_agent(example_prompts=["restart the worker"]))
    assert _has_router_wording(_agent(router_trigger_prompt="handle things"))


def test_an_identity_route_skip_on_a_channel_is_not_told_to_route_to_a_contact() -> None:
    """The `identity_route` remedy is forward-looking, so it follows the origin.

    Its only producer was channel Pass 1's own `is_identity` branch, which the
    scope split deleted — so every row carrying it is history, and "route to the
    contact rather than to their agent" is an instruction about a candidate
    class a channel no longer has. The explanation may keep describing what
    happened; the action cannot.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_IDENTITY_ROUTE, origin=routing_trace.ORIGIN_SERVER_CHANNEL
    )

    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: it "
        "was reached through an identity contact route, which hands off to that "
        "person's agents in a second stage and was never selectable from a "
        "channel. Give this user their own install of the agent and set a "
        "router trigger prompt (or example prompts) on it — a channel routes "
        "over the sender's own agents and reads no identity contact at all."
    )


def test_a_bundle_deleted_mid_decision_is_not_told_to_publish_itself() -> None:
    """`bundle_missing` exists so this case stops borrowing `no_revision`.

    Pass 2 re-loads its winner by id in its own session — the ballot may have
    been scanned in Pass 1's, which is closed — so a bundle deleted in between
    is genuinely reachable. Reported as `no_revision` it would read "its bundle
    has no resolvable published revision / **Publish the bundle**", which is
    advice about a bundle that is published and gone.
    """
    diagnosis = _skip_verdict(
        routing_trace.SKIP_BUNDLE_MISSING,
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        kind=routing_trace.KIND_BUNDLE,
    )

    assert "Publish the bundle" not in diagnosis.verdict
    assert diagnosis.verdict == (
        "Expected Agent was considered for this decision and then excluded: it "
        "won the auto-install pass and then could not be loaded — the bundle "
        "was deleted between this decision's catalog scan and the lookup that "
        "follows it. Re-run this decision. Nothing is wrong with the routing "
        "rules: the bundle that matched simply stopped existing mid-decision."
    )
