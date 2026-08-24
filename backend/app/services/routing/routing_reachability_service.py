"""The reachability verdict — why a decision went the way it did, in words.

Plan §9's headline output, and the whole answer to the motivating bug (§2,
Bug 2): a standalone agent has no ``AppAgentRoute``, is therefore absent from
``get_effective_routes_for_user``, and the classifier never sees it. That is
**not** fixed by changing the auto-route rule — standalone agents are
deliberately owner-managed — it is fixed by saying so, out loud, to the admin
staring at a ``no_match``:

    This user has 3 effective routes; Equation Assistant is not among them
    because it is not a bundle install and has no App MCP route — add one from
    its Integrations tab.

**Why the sentence is built here and not in the card** (plan §10, Phase 4).
Three reasons, and the third is the one that matters:

1. It is testable. Every branch below has a test asserting the sentence it
   emits; wording that lives in TSX has nothing that fails when it goes wrong.
2. It lives with the rules it paraphrases. The clause "it is not a bundle
   install and has no App MCP route" is a restatement of
   ``AppAgentRouteService._create_auto_route_for_agent``'s two ``None``
   conditions. Two files apart, they drift; one import apart, the drift is
   visible.
3. A wrong diagnosis is worse than none. This surface tells an admin what to
   change on somebody else's agent. A sentence assembled from whichever fields
   the card happened to have would be confidently wrong on the branches nobody
   thought about, and it would look exactly like the branches that are right.

**Two sources of truth, deliberately, in this order.** A trace records what the
router actually considered; the database records what is configured *now*. The
trace wins whenever it has an answer, because it describes the decision being
diagnosed — the route may well have been fixed since. The database is consulted
only for an agent the trace never mentions, which is precisely the case the
trace cannot explain: it has no row for something that was never a candidate.

**What this reads about the expected agent is an allowlist of two fields** —
its name and its owner's email. Both clear the bar §7 sets for
``candidates[].trigger_prompt``: owner-authored configuration, not
sender-derived, already visible to this (superuser-only) audience. Nothing else
about the agent reaches the response, and a third field would have to clear the
same bar when it is added rather than inherit these two's answer.

**Near-miss ranking reuses ``AppAgentRouteService``'s Jaccard helpers by
calling them** (plan §3: "Reuse verbatim"). They are private —
``_tokens_for_similarity`` / ``_jaccard_similarity`` — and reaching across a
service boundary for an underscore-prefixed static method is not something to
do casually. It is still strictly better than the alternative here: a second
copy of the tokenizer would make "closest: X 0.31" and install-time conflict
detection disagree about what a token is, silently, the first time either was
tuned. Flagged rather than laundered; if a third caller appears, promote them.

**Never break the thing it observes** (§11a Rule 2). This module runs on the
read path only — no capture is open, no routing decision is in flight — so a
failure here costs a diagnosis, not a delivery. :meth:`diagnose` is still
total, because its caller is ``RoutingTraceService.get``, which serves the
whole trace: a diagnosis that raised would take the trace detail with it, and
the trace is the more valuable half.

See ``docs/plans/auto_routing_tuning_plan.md`` §2, §9 and §10 Phase 4.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session as DBSession, select

from app.models import Agent, User
from app.models.app_mcp.app_agent_route import (
    AppAgentRoute,
    AppAgentRouteAssignment,
    UserAppAgentRoute,
)
from app.models.routing.routing_decision import (
    RoutingDecisionPublic,
    RoutingDiagnosisPublic,
    RoutingNearMiss,
)
from app.services.app_mcp.app_agent_route_service import AppAgentRouteService
from app.services.routing import routing_trace

logger = logging.getLogger(__name__)


# ── Verdict codes ────────────────────────────────────────────────────
#
# One code per sentence, and that pairing is load-bearing: a test pins the code
# *and* the sentence, so a reworded verdict that no longer answers its branch
# fails rather than passing under a code that still looks right. Plain strings,
# matching the feature's convention — readers tolerate unknown values.

#: No expected agent named.
CODE_ROUTED = "routed"
CODE_ERROR = "error"
CODE_NO_CANDIDATES = "no_candidates"
CODE_ALL_CANDIDATES_SKIPPED = "all_candidates_skipped"
CODE_NO_MATCH = "no_match"

#: An expected agent was named and the trace has a row for it.
CODE_EXPECTED_SELECTED = "expected_agent_selected"
CODE_EXPECTED_CONSIDERED = "expected_agent_considered"
CODE_EXPECTED_SKIPPED = "expected_agent_skipped"

#: An expected agent was named and the trace has no row for it, so the answer
#: comes from current configuration instead.
CODE_EXPECTED_UNKNOWN = "expected_agent_unknown"
CODE_EXPECTED_STANDALONE_NO_ROUTE = "expected_agent_standalone_no_route"
CODE_EXPECTED_BUNDLE_NO_ROUTE = "expected_agent_bundle_no_route"
CODE_EXPECTED_NO_TRIGGER_PROMPT = "expected_agent_no_trigger_prompt"
CODE_EXPECTED_FOREIGN_OWNER = "expected_agent_foreign_owner"
CODE_EXPECTED_ROUTE_INACTIVE = "expected_agent_route_inactive"
CODE_EXPECTED_ROUTE_NOT_APP_MCP = "expected_agent_route_not_app_mcp"
CODE_EXPECTED_ROUTE_UNASSIGNED = "expected_agent_route_unassigned"
CODE_EXPECTED_LOOKS_REACHABLE = "expected_agent_looks_reachable"

#: The diagnosis could not be computed at all.
CODE_UNAVAILABLE = "unavailable"


#: Why a near-miss ranking is missing. Named rather than left as an empty list:
#: an empty ranking under a ``no_match`` reads as "nothing came close", which is
#: a finding, and it must not be indistinguishable from "we could not measure".
#: §11a Rule 1 on a read surface.
NEAR_MISS_NO_TEXT_NOTICE = (
    "Near-miss ranking needs the message that was routed, and this trace's "
    "text is not available — ROUTING_TRACE_STORE_MESSAGE_TEXT is off now, or "
    "was off when it was captured. The candidate list and the verdict are "
    "unaffected."
)
NEAR_MISS_NO_CANDIDATES_NOTICE = (
    "Nothing to rank: this decision considered no candidate with a trigger "
    "prompt or example messages."
)

#: How many near-misses to return. The card shows a short list and the tail of
#: a Jaccard ranking is noise — every unrelated agent scores a little above zero
#: on stopword overlap.
NEAR_MISS_LIMIT = 5


# ── Skip-reason explanations ─────────────────────────────────────────
#
# Keyed by ``routing_trace``'s constants, never by literals, so renaming one
# breaks the import rather than silently falling through to the unknown branch.
# The fallback below is what makes this safe to leave incomplete: an unmapped
# reason is reported by name with an honest "this build has no explanation for
# it", which is a worse diagnosis but never a wrong one.

_SKIP_EXPLANATIONS: dict[str, tuple[str, str]] = {
    routing_trace.SKIP_ROUTE_INACTIVE: (
        "its App MCP route exists but is switched off",
        "Switch the route back on from the agent's Integrations tab.",
    ),
    routing_trace.SKIP_FOREIGN_OWNER: (
        "it belongs to a different account, and a channel session must run on "
        "the sender's own install",
        "Share the agent's bundle with this user and have them install it — "
        "routing to somebody else's install is refused by design.",
    ),
    routing_trace.SKIP_IDENTITY_ROUTE: (
        "it was reached through an identity contact route, which hands off to "
        "that person's agents in a second stage and is not selectable from a "
        "channel",
        "Route to the contact rather than to their agent, or give this user "
        "their own install of it.",
    ),
    routing_trace.SKIP_NO_TRIGGER_PROMPT: (
        "it has no router trigger prompt, so the classifier had nothing to "
        "match the message against",
        "Set a router trigger prompt on the agent's Configuration tab (for a "
        "bundle, on the revision that gets published).",
    ),
    routing_trace.SKIP_ALREADY_INSTALLED: (
        "this user already has it installed, so the auto-install pass passed "
        "over it — it should have been reachable as one of their own routes "
        "instead",
        "Check the installed agent's App MCP route: an install whose route is "
        "missing or switched off falls into exactly this gap.",
    ),
    routing_trace.SKIP_NOT_INSTALLABLE: (
        "this user is not allowed to install its bundle",
        "Make the bundle public, or grant this user access to it, before "
        "expecting the auto-install pass to reach it.",
    ),
    routing_trace.SKIP_NO_REVISION: (
        "its bundle has no resolvable published revision",
        "Publish the bundle — an entry on the auto-install list with nothing "
        "published behind it can never win.",
    ),
    routing_trace.SKIP_AGENT_MISSING: (
        "the route that matched points at an agent id with no agent behind it",
        "Delete the dangling route and recreate it from the agent's "
        "Integrations tab.",
    ),
}


class RoutingReachabilityService:
    """Plain-language verdicts and near-miss ranking for one stored decision."""

    @staticmethod
    def diagnose(
        db: DBSession,
        trace: RoutingDecisionPublic,
        *,
        expected_agent_id: uuid.UUID | None = None,
    ) -> RoutingDiagnosisPublic:
        """The verdict for ``trace``, optionally about one expected agent.

        Total: any failure comes back as :data:`CODE_UNAVAILABLE` rather than
        propagating. See the module docstring — the caller is serving the whole
        trace, and losing the diagnosis is much cheaper than losing that.

        ``trace`` is the **projected** public model, not the row. That is not
        incidental: it means the diagnosis sees exactly what the reader sees, so
        it can never quote a field the message-text gate withheld, and the
        near-miss ranking goes quiet on its own when the text is gated instead
        of needing a second rule that remembers to.
        """
        try:
            return _diagnose(db, trace, expected_agent_id=expected_agent_id)
        except Exception:  # noqa: BLE001 — a diagnostic must not break the read
            logger.warning("Routing reachability diagnosis failed", exc_info=True)
            # Composed from problem + action like every other branch, and not
            # written out twice: the "action is a substring of verdict"
            # invariant has to hold on the failure path too, or a client that
            # renders them separately would show a remedy the sentence does not
            # contain exactly when something has already gone wrong.
            problem = (
                "This decision's diagnosis could not be computed, but the "
                "trace itself is intact."
            )
            action = (
                "Read the candidate list below, and see the server logs for "
                "why the summary failed."
            )
            return RoutingDiagnosisPublic(
                code=CODE_UNAVAILABLE,
                verdict=f"{problem} {action}",
                action=action,
                expected_agent_id=expected_agent_id,
            )


# ── Implementation ───────────────────────────────────────────────────


def _diagnose(
    db: DBSession,
    trace: RoutingDecisionPublic,
    *,
    expected_agent_id: uuid.UUID | None,
) -> RoutingDiagnosisPublic:
    candidates = _candidates(trace.stages)
    eligible = [c for c in candidates if c.get("eligible")]
    skipped_by_reason: dict[str, int] = {}
    for candidate in candidates:
        if candidate.get("eligible"):
            continue
        reason = str(candidate.get("skip_reason") or "unspecified")
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1

    near_misses, near_miss_notice = _rank_near_misses(
        trace.message_text, candidates
    )

    if expected_agent_id is None:
        code, problem, action = _general_verdict(trace, eligible, candidates)
        name = owner_email = None
    else:
        code, problem, action, name, owner_email = _expected_agent_verdict(
            db, trace, candidates, eligible, expected_agent_id, near_misses
        )

    return RoutingDiagnosisPublic(
        code=code,
        # Composed, never written twice: ``action`` is a substring of
        # ``verdict`` by construction, so a client rendering the two separately
        # cannot show a remedy the sentence disagrees with.
        verdict=f"{problem} {action}".strip(),
        action=action,
        eligible_candidate_count=len(eligible),
        skipped_by_reason=skipped_by_reason,
        expected_agent_id=expected_agent_id,
        expected_agent_name=name,
        expected_agent_owner_email=owner_email,
        near_misses=near_misses,
        near_miss_notice=near_miss_notice,
    )


def _general_verdict(
    trace: RoutingDecisionPublic,
    eligible: list[dict],
    candidates: list[dict],
) -> tuple[str, str, str]:
    """The verdict with no expected agent named: (code, problem, action)."""
    if trace.outcome == routing_trace.OUTCOME_ERROR:
        return (
            CODE_ERROR,
            f"This decision failed before it reached a verdict: "
            f"{trace.error or 'no error was recorded'}.",
            "Check the provider attempts below — a routing failure with no "
            "attempt at all means no AI credential was usable, which is a "
            "server configuration problem rather than an agent one.",
        )

    if trace.outcome == routing_trace.OUTCOME_ROUTED:
        chosen = (
            trace.selected_agent_name
            or trace.selected_bundle_name
            or "the selected agent"
        )
        return (
            CODE_ROUTED,
            f"This message routed to {chosen}, chosen from "
            f"{_count(len(eligible), 'eligible candidate')}.",
            "Nothing to fix here. If it reached the wrong agent, compare the "
            "near-miss scores below and tighten the winner's trigger prompt "
            "so it stops claiming this kind of message.",
        )

    if not candidates:
        return (
            CODE_NO_CANDIDATES,
            "This user had no routing candidates at all: no App MCP route "
            "reaches them and no auto-install bundle was eligible, so no "
            "message from them can route anywhere.",
            "Give the agent you expected an App MCP route from its "
            "Integrations tab, or add its bundle to the auto-install list.",
        )

    if not eligible:
        one = len(candidates) == 1
        return (
            CODE_ALL_CANDIDATES_SKIPPED,
            f"This user has no eligible routes: "
            f"{_count(len(candidates), 'candidate')} "
            f"{'was' if one else 'were all'} excluded before the classifier "
            f"saw {'it' if one else 'them'} ({_reasons(candidates)}).",
            "Fix the exclusion on the agent you expected — the candidate "
            "table below names the reason for each one.",
        )

    return (
        CODE_NO_MATCH,
        f"This user has {_count(len(eligible), 'effective route')} and the "
        f"classifier matched none of them.",
        "Widen the trigger prompt of the agent that should have won — the "
        "near-miss scores below say which came closest — or use Draft a "
        "recommendation to generate wording for its owner.",
    )


def _expected_agent_verdict(
    db: DBSession,
    trace: RoutingDecisionPublic,
    candidates: list[dict],
    eligible: list[dict],
    expected_agent_id: uuid.UUID,
    near_misses: list[RoutingNearMiss],
) -> tuple[str, str, str, str | None, str | None]:
    """The verdict about one named agent: (code, problem, action, name, email).

    The trace is consulted first and the database only for an agent the trace
    never mentions — see the module docstring on why that order and not the
    reverse.
    """
    row = _find_candidate(candidates, expected_agent_id)
    agent = db.get(Agent, expected_agent_id)
    name = _agent_label(row, agent, expected_agent_id)
    owner_email = _owner_email(db, row, agent)

    if row is not None:
        return (*_verdict_from_trace(trace, row, name, near_misses), name, owner_email)

    if agent is None:
        return (
            CODE_EXPECTED_UNKNOWN,
            f"No agent {expected_agent_id} exists on this server, so it could "
            f"never have been a routing candidate.",
            "Check the id against the agent's own page — a deleted agent and "
            "a mistyped id look the same from here.",
            None,
            None,
        )

    prefix = (
        f"This user has {_count(len(eligible), 'effective route')}; "
        f"{name} is not among them because"
    )
    code, problem, action = _verdict_from_configuration(db, trace, agent, prefix)
    return code, problem, action, name, owner_email


def _verdict_from_trace(
    trace: RoutingDecisionPublic,
    row: dict,
    name: str,
    near_misses: list[RoutingNearMiss],
) -> tuple[str, str, str]:
    """The agent WAS in this decision's candidate list. Say what happened to it."""
    if not row.get("eligible"):
        reason = str(row.get("skip_reason") or "")
        explanation, action = _SKIP_EXPLANATIONS.get(
            reason,
            (
                f"it was excluded with reason '{reason or 'unspecified'}', "
                f"which this build has no explanation for",
                "Read the candidate row below and the router's logs — this "
                "reason was added after the diagnosis was written.",
            ),
        )
        return (
            CODE_EXPECTED_SKIPPED,
            f"{name} was considered for this decision and then excluded: "
            f"{explanation}.",
            action,
        )

    selected = str(trace.selected_agent_id or "")
    if selected and selected == str(row.get("ref_id") or ""):
        return (
            CODE_EXPECTED_SELECTED,
            f"{name} is the agent this decision chose.",
            "Nothing to fix — if the message still went nowhere, the failure "
            "is after routing (session setup or the outbound reply), not in "
            "it.",
        )

    closest = next(
        (m for m in near_misses if m.ref_id == str(row.get("ref_id") or "")), None
    )
    score = f" (token overlap {closest.similarity:.2f})" if closest else ""
    return (
        CODE_EXPECTED_CONSIDERED,
        f"{name} was an eligible candidate{score} and the classifier did not "
        f"pick it — reachability is not the problem here.",
        "Widen its trigger prompt to cover wording like this message, or use "
        "Draft a recommendation to generate that wording for its owner.",
    )


def _verdict_from_configuration(
    db: DBSession,
    trace: RoutingDecisionPublic,
    agent: Agent,
    prefix: str,
) -> tuple[str, str, str]:
    """The agent was never a candidate. Explain from what is configured now.

    Checked in this order, and the order is the diagnosis:

    1. **A route that exists but is misconfigured**, narrowest first — reachable
       (so the trace is simply older than the fix), then unassigned, then off
       the App MCP channel, then switched off. Each is a one-switch repair and
       each is a *different* switch, so a coarser answer would send the reader
       to the wrong control.
    2. **No route at all, and the agent belongs to somebody else.** Ahead of the
       bundle/standalone split because the repair is not "add a route" — this
       user cannot add one to an agent they do not own.
    3. **No route at all, on the user's own agent** — standalone (plan §2's
       Bug 2), then bundle-install-with-no-trigger-prompt, then
       bundle-install-with-one. Three genuinely different repairs, which is why
       they are three branches and not one "it has no route".
    """
    routes = list(
        db.exec(select(AppAgentRoute).where(AppAgentRoute.agent_id == agent.id)).all()
    )
    personal = list(
        db.exec(
            select(UserAppAgentRoute).where(
                UserAppAgentRoute.agent_id == agent.id,
                UserAppAgentRoute.user_id == trace.user_id,
            )
        ).all()
    )
    assigned_route_ids = set()
    if routes and trace.user_id is not None:
        assigned_route_ids = {
            a.route_id
            for a in db.exec(
                select(AppAgentRouteAssignment).where(
                    AppAgentRouteAssignment.user_id == trace.user_id,
                    AppAgentRouteAssignment.is_enabled == True,  # noqa: E712
                )
            ).all()
        }

    reachable = [
        r
        for r in routes
        if r.is_active and r.channel_app_mcp and r.id in assigned_route_ids
    ] + [p for p in personal if p.is_active and p.channel_app_mcp]
    if reachable:
        return (
            CODE_EXPECTED_LOOKS_REACHABLE,
            f"{prefix} it was not a candidate when this decision ran, even "
            f"though its App MCP route looks correctly configured now.",
            "Re-run this decision — a route added or switched on after the "
            "trace was captured explains exactly this, and the re-run will "
            "show it as a candidate.",
        )

    unassigned = [r for r in routes if r.is_active and r.channel_app_mcp]
    if unassigned:
        return (
            CODE_EXPECTED_ROUTE_UNASSIGNED,
            f"{prefix} its App MCP route is not assigned to this user, or the "
            f"assignment is switched off.",
            "Assign this user to the route from the route's Users list, and "
            "check the per-user toggle is on.",
        )

    not_app_mcp = [r for r in routes if r.is_active] + [
        p for p in personal if p.is_active
    ]
    if not_app_mcp:
        return (
            CODE_EXPECTED_ROUTE_NOT_APP_MCP,
            f"{prefix} its route is not enabled for the App MCP channel, which "
            f"is the channel this decision routed over.",
            "Turn on App MCP for that route from the agent's Integrations tab.",
        )

    if routes or personal:
        return (
            CODE_EXPECTED_ROUTE_INACTIVE,
            f"{prefix} its App MCP route exists but is switched off.",
            "Switch the route back on from the agent's Integrations tab.",
        )

    if trace.user_id is not None and agent.owner_id != trace.user_id:
        return (
            CODE_EXPECTED_FOREIGN_OWNER,
            f"{prefix} it belongs to a different account and has no route "
            f"assigning it to this user — a channel session runs on the "
            f"sender's own install.",
            "Share its bundle with this user and have them install it, or "
            "assign them to an App MCP route on it.",
        )

    if agent.bundle_uuid is None:
        # Plan §2, Bug 2 — and §9's sentence, close to verbatim. The parenthesis
        # exists because the obvious next move is wrong: setting the trigger
        # prompt is what mints a route for a bundle install
        # (``sync_router_trigger_prompt_from_agent``) and does nothing at all
        # here, so an admin who tried that first would conclude the feature is
        # broken rather than that this path does not exist.
        return (
            CODE_EXPECTED_STANDALONE_NO_ROUTE,
            f"{prefix} it is not a bundle install and has no App MCP route.",
            "Add one from its Integrations tab. (Setting its router trigger "
            "prompt alone will not do it: a standalone agent never gets an "
            "automatic route — that is deliberate, its owner manages App MCP "
            "exposure explicitly.)",
        )

    if not (agent.router_trigger_prompt or "").strip():
        return (
            CODE_EXPECTED_NO_TRIGGER_PROMPT,
            f"{prefix} it has no router trigger prompt, so no route was ever "
            f"created for its install and the classifier would have had "
            f"nothing to match on anyway.",
            "Set a router trigger prompt on the agent's Configuration tab — "
            "for a bundle install that creates the App MCP route "
            "automatically.",
        )

    return (
        CODE_EXPECTED_BUNDLE_NO_ROUTE,
        f"{prefix} it has no App MCP route, even though its router trigger "
        f"prompt is set.",
        "Add a route from its Integrations tab. An install whose revision "
        "carried no trigger prompt at the time gets no automatic route, and "
        "re-saving the prompt is what normally backfills it.",
    )


# ── Near-miss ranking ────────────────────────────────────────────────


def _rank_near_misses(
    message: str | None, candidates: list[dict]
) -> tuple[list[RoutingNearMiss], str | None]:
    """Rank candidates by token overlap with the message. See module docstring.

    ``AppAgentRouteService._tokens_for_similarity`` and ``._jaccard_similarity``
    are **called**, not copied: install-time conflict detection and this ranking
    have to agree on what a token is, and two copies of a tokenizer agree only
    until one of them is tuned.

    Ranked on ``trigger_prompt`` **and** ``prompt_examples`` together, because
    together is what the classifier now receives. Phase 5 fixed Bug 1 — examples
    reach the rendered prompt — and this ranking moved in the same pass on
    purpose: a card that scores on a strict subset of what the model saw is this
    feature misreporting the system on its own diagnostic surface. An agent
    whose trigger prompt is vague and whose examples are exact would have been
    ranked last while actually winning the route.

    Scored as one concatenated document rather than as a max over two scores:
    Jaccard is over token *sets*, so joining them is the same question the
    classifier is answering ("does anything this owner wrote look like this
    message"), and it does not privilege whichever field happens to be shorter.
    """
    text = (message or "").strip()
    if not text:
        return [], NEAR_MISS_NO_TEXT_NOTICE

    message_tokens = AppAgentRouteService._tokens_for_similarity(text)
    ranked: list[RoutingNearMiss] = []
    for candidate in candidates:
        prompt = str(candidate.get("trigger_prompt") or "")
        examples = str(candidate.get("prompt_examples") or "")
        ref_id = str(candidate.get("ref_id") or "")
        if not (prompt or examples) or not ref_id:
            continue
        scored_text = "\n".join(part for part in (prompt, examples) if part)
        similarity = AppAgentRouteService._jaccard_similarity(
            message_tokens, AppAgentRouteService._tokens_for_similarity(scored_text)
        )
        ranked.append(
            RoutingNearMiss(
                ref_id=ref_id,
                kind=str(candidate.get("kind") or ""),
                name=str(candidate.get("name") or ref_id),
                similarity=round(similarity, 2),
                eligible=bool(candidate.get("eligible")),
                skip_reason=candidate.get("skip_reason"),
            )
        )
    if not ranked:
        return [], NEAR_MISS_NO_CANDIDATES_NOTICE

    # Name as the tiebreak, so a page of equally-scoring candidates renders in
    # a stable order instead of whatever the stage payload happened to hold.
    ranked.sort(key=lambda m: (-m.similarity, m.name))
    return ranked[:NEAR_MISS_LIMIT], None


# ── Small helpers ────────────────────────────────────────────────────


def _candidates(stages: Any) -> list[dict]:
    """Every candidate across every stage, de-duplicated by ``ref_id``.

    Defensive throughout, like the other ``stages`` readers: this is JSONB
    written by a recorder whose dataclasses keep changing, and a row written by
    an older build must not break a diagnosis.

    De-duplicated because a candidate can legitimately appear in more than one
    stage, and "3 effective routes" counted twice is a wrong number stated with
    confidence. The **last** occurrence wins: ``mark_candidate_skipped`` flips a
    row in place, so a later stage carries the more settled verdict.
    """
    found: dict[str, dict] = {}
    for stage in stages or []:
        for candidate in (stage or {}).get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            ref = str(candidate.get("ref_id") or "")
            if not ref:
                continue
            found[ref] = candidate
    return list(found.values())


def _find_candidate(candidates: list[dict], ref_id: uuid.UUID) -> dict | None:
    wanted = str(ref_id)
    return next(
        (c for c in candidates if str(c.get("ref_id") or "") == wanted), None
    )


def _agent_label(row: dict | None, agent: Agent | None, ref_id: uuid.UUID) -> str:
    """A display name for the expected agent, from whichever source has one.

    **The agent row first, the trace second.** The reverse order was tried and
    is wrong: a candidate's ``name`` is whatever the router labelled that
    candidate with, and for an identity contact route that is the *contact's*
    name, not the agent's — so a verdict about an agent id came back saying
    "someone@example.com was considered and then excluded", conflating a person
    with an agent. The caller asked about an agent id; the sentence names that
    agent. The trace's label is the fallback for an agent row that no longer
    exists, where it is the only name anybody has.
    """
    if agent is not None and (agent.name or "").strip():
        return agent.name
    if row is not None:
        name = str(row.get("name") or "").strip()
        if name:
            return name
    return str(ref_id)


def _owner_email(db: DBSession, row: dict | None, agent: Agent | None) -> str | None:
    if row is not None and row.get("owner_email"):
        return str(row["owner_email"])
    if agent is None:
        return None
    owner = db.get(User, agent.owner_id)
    return owner.email if owner is not None else None


def _count(n: int, noun: str) -> str:
    """``"3 effective routes"`` / ``"1 effective route"``.

    Pluralised rather than "1 effective route(s)": this sentence is the feature
    for the motivating case and it is read by a person.
    """
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _reasons(candidates: list[dict]) -> str:
    """``"foreign_owner, route_inactive"`` — the skip reasons, deduped, sorted."""
    reasons = sorted(
        {
            str(c.get("skip_reason") or "unspecified")
            for c in candidates
            if not c.get("eligible")
        }
    )
    return ", ".join(reasons) or "no reason recorded"
