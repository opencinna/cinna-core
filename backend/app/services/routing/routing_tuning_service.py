"""Simulate, replay and recommendation-drafting for the admin tuning surface.

The read/write half of routing observability lives in ``routing_trace_service``;
this module is the *interactive* half — the three operations that make a stored
decision something an admin can poke at rather than only read.

Three properties hold this together, and each is load-bearing:

**Simulate has no side effects, structurally.** It calls
:meth:`ChannelRoutingService.decide` and stops. ``decide`` creates no thread
binding, no session, no install and sends no reply — not because a flag
suppresses those, but because nothing reachable from it can do them (see that
module's docstring). There is no ``simulate=True`` parameter anywhere in this
feature, deliberately: a flag makes the safety property something somebody has
to keep true at every branch forever, and this way it is a property of the call
graph.

**A simulate response exposes exactly what a stored trace exposes.** Not
"the same fields" — the *same function*. :meth:`simulate` persists the decision
and then returns ``RoutingTraceService.get(db, trace_id)``, which is literally
what ``GET /admin/routing/traces/{id}`` returns. So the message-text gate, the
``SAFE_STAGE_FIELDS`` allowlist, the name resolution and the notices all apply
to a simulate by construction. A hand-rolled projection here would start
identical, drift on the next field added to one side, and the drift would be
silent — which is exactly the inventory failure the allowlist exists to prevent
one layer down. If this ever stops calling ``RoutingTraceService.get``, the
guarantee stops being a guarantee and becomes a comment.

**A simulate decides under the real channel policy.** Where the run names a
channel — always, on a replay of a channel trace — the sender's resolved policy
is read from the live row and handed to ``decide``, so the scope, the pin and
the auto-install gate are the ones the webhook would apply. Only a hand-typed
simulate with no channel gets the explicit no-channel policy. See
:meth:`RoutingTuningService._policy_for`; a permissive default there would make
this module's central claim — that it reproduces the real path — quietly false.

**Note what ``decide`` *does* write.** On its error path — and only there —
each routing pass persists its own ``outcome=error`` trace before re-raising,
because a trace the caller never receives is otherwise the one outcome no code
path can produce. That is the diagnostic, not an effect on the user's account,
and :meth:`simulate` accounts for it: it catches the re-raise and points the
admin at the row rather than swallowing the failure or returning a bare 500.

**Nothing here writes to another user's agent.** :meth:`recommend` generates
text and returns it. That is the feature's hard boundary (plan §1): when a
foreign agent routes badly the output is a copyable recommendation for its
owner, never an edit.

Access control, rate limiting and the audit row are the route's job, not this
module's — see ``app/api/routes/admin_routing.py``. What is *not* the route's
job is the reasoning about why they are required; that lives with the
``ROUTING_SIMULATE_RUN`` constant.

See ``docs/plans/auto_routing_tuning_plan.md`` §6 and §12.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session as DBSession

from app.models.routing.routing_decision import (
    RoutingDecision,
    RoutingDecisionPublic,
    RoutingRecommendationPublic,
    RoutingReplayDiff,
)
from app.models import ServerChannel
from app.services.routing import routing_trace
from app.services.routing.routing_trace_service import RoutingTraceService
from app.services.server_channels.channel_policy_service import (
    ChannelPolicyService,
    ResolvedChannelPolicy,
)
from app.services.server_channels.channel_routing_service import (
    ChannelRoutingService,
)

logger = logging.getLogger(__name__)


class RoutingSimulationError(Exception):
    """A simulate/replay could not run, or ran and could not be served.

    Carries the HTTP status the route should use. A dedicated exception rather
    than raising ``HTTPException`` from a service: the status is part of the
    *contract* here (there are four genuinely different refusals and they are
    not interchangeable), but the service must stay callable without FastAPI in
    the frame.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ReplaySource:
    """What a replay needs from a stored decision, detached from the ORM row.

    :meth:`RoutingTuningService.replay_source` is the one place that reads a
    ``routing_decision`` row *around* the read projection — a replay needs the
    original message, and withholding it is precisely that projection's job. So
    the bypass hands back plain values rather than the row: the route never
    holds an instance whose ``message_text`` could reach a response builder, and
    it never has to remember that the audit's commit expires ORM attributes
    underneath it. Frozen, so nothing downstream can edit it back toward the
    row it came from.
    """

    message: str
    user_id: uuid.UUID
    channel_id: uuid.UUID | None
    thread_key: str | None


class RoutingTuningService:
    """Simulate a message, replay a stored decision, draft a recommendation."""

    # ── Simulate ─────────────────────────────────────────────────────

    @staticmethod
    async def simulate(
        db: DBSession,
        *,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        message: str,
        include_catalog: bool = True,
        channel_id: uuid.UUID | None = None,
        thread_key: str | None = None,
    ) -> RoutingDecisionPublic:
        """Route ``message`` for ``user_id`` with no effects; return the trace.

        The decision is persisted with ``origin="simulate"`` and
        ``actor_user_id`` set, then read back through
        :meth:`RoutingTraceService.get` — the same call the trace-detail route
        makes, which is what makes "a simulate reveals exactly what a stored
        trace reveals" a fact about the code rather than a claim about it.

        Persisting is not a side effect *of the routing decision*: it is the
        result being returned, and it is the row the ``ROUTING_SIMULATE_RUN``
        audit entry points at. It is also why the caller must refuse the run
        when ``ROUTING_TRACE_ENABLED`` is off — with no row there is nothing to
        read back, and inventing a response for that case would mean building
        the second projection this docstring exists to forbid.

        The channel policy is resolved **before** the run and from the real
        row, whenever a channel is named — see :meth:`_policy_for`. A simulate
        that routed over a different candidate set than the webhook would is
        not a simulation of anything.
        """
        policy = RoutingTuningService._policy_for(
            db, user_id=user_id, channel_id=channel_id
        )
        try:
            decision = await ChannelRoutingService.decide(
                user_id=user_id,
                text=message,
                policy=policy,
                include_catalog=include_catalog,
                origin=routing_trace.ORIGIN_SIMULATE,
                channel_id=channel_id,
                thread_key=thread_key,
                actor_user_id=actor_user_id,
            )
        except Exception as exc:  # noqa: BLE001
            # ``decide`` re-raises whatever the routing pass raised, and it does
            # so *after* the failing pass has already persisted its own
            # ``outcome=error`` row (the thread targets each write the trace the
            # caller will never get to see, then re-raise — see their ``except``
            # blocks). Left uncaught this surfaced as a bare FastAPI 500: the
            # single most useful row this table can hold had just been written,
            # and the admin was told nothing about it.
            #
            # Deliberately NOT solved by making ``decide`` swallow and return.
            # The real path depends on that exception reaching
            # ``_route_new_thread``'s handler to send ``REPLY_SETUP_FAILED``;
            # a diagnostic route's ergonomics must not change what an external
            # sender is told. So the row is pointed at instead of returned —
            # ``origin=simulate`` plus ``outcome=error`` narrows the trace list
            # to it, and the ``ROUTING_SIMULATE_RUN`` audit entry timestamps the
            # run.
            logger.warning("Routing simulate failed during decide", exc_info=True)
            raise RoutingSimulationError(
                500,
                "The routing pass itself failed: "
                f"{routing_trace.describe_exception(exc)}. Its error trace was "
                "recorded — find it with "
                "GET /api/v1/admin/routing/traces?origin=simulate&outcome=error.",
            ) from exc
        # Offloaded for the same reason the real path offloads it: ``persist``
        # is synchronous and takes its own pooled connection to INSERT+COMMIT.
        # ``persist_call`` rather than a hand-built partial, so simulate and the
        # real path cannot disagree about which trace is the terminal one.
        trace_id = await ChannelRoutingService.run_in_thread(decision.persist_call())
        if trace_id is None:
            # ``persist`` swallows its own failures and returns ``None``. The
            # run happened and cost real LLM spend, so this is reported rather
            # than retried or faked.
            raise RoutingSimulationError(
                500,
                "The simulation ran but its routing trace could not be stored, "
                "so there is nothing to show. See the server logs; the "
                "ROUTING_SIMULATE_RUN audit entry records that the run "
                "happened.",
            )
        result = RoutingTraceService.get(db, trace_id)
        if result is None:
            raise RoutingSimulationError(
                500,
                "The simulation ran and was stored but could not be read back.",
            )
        return result

    @staticmethod
    def _policy_for(
        db: DBSession, *, user_id: uuid.UUID, channel_id: uuid.UUID | None
    ) -> ResolvedChannelPolicy:
        """The channel policy this run must decide under.

        **Resolved, never assumed.** Simulate and replay exist to reproduce the
        real path, and the policy is now part of what the real path decides
        with: the sender's agent scope, their pinned agent, and whether the
        auto-install pass may run. A simulate that took a permissive default
        would show an admin a candidate set the webhook would never build, and
        the divergence would be invisible — the trace it returns looks exactly
        like a real one. This is the same reasoning that put
        ``include_catalog`` inside Pass 1 rather than only around Pass 2 (see
        ``ChannelRoutingService.decide``): a reproduction that quietly answers
        an easier question is worse than no reproduction.

        Three cases, and only the first is a policy this module invents:

        - **No channel named.** A hand-typed ``POST /admin/routing/simulate``
          has no ``ServerChannel`` to resolve against; that is the one case
          ``ResolvedChannelPolicy.for_no_channel`` exists for, and its
          docstring is the argument for why it is not spelled as a default.
        - **A channel that resolves.** The real policy, for this target user —
          the identical call the webhook makes.
        - **A channel id with no row behind it.** A pure race, and a narrow
          one: ``RoutingDecision.channel_id`` is ``ON DELETE CASCADE``, so a
          deleted channel takes its traces with it and ``replay_source`` would
          have 404'd on the row first. Degraded to the no-channel policy and
          logged, rather than raised: the run is a diagnostic, the sender's
          account and agents are still real, and refusing outright would turn a
          millisecond-wide race into an error an admin cannot act on.
        """
        if channel_id is None:
            return ResolvedChannelPolicy.for_no_channel()
        channel = db.get(ServerChannel, channel_id)
        if channel is None:
            logger.warning(
                "Routing simulate/replay names channel %s, which no longer "
                "exists; deciding without a channel policy",
                channel_id,
            )
            return ResolvedChannelPolicy.for_no_channel()
        return ChannelPolicyService.resolve(db, channel, user_id)

    # ── Replay ───────────────────────────────────────────────────────

    @staticmethod
    def replay_source(db: DBSession, trace_id: uuid.UUID) -> ReplaySource:
        """The stored row a replay will re-run, or a refusal explaining why not.

        Deliberately reads the **row**, not :meth:`RoutingTraceService.get`'s
        projection: a replay needs the original message text, and the
        projection's whole job is to withhold it. Kept in the service rather
        than done in the route so the one place that bypasses the read
        projection is the one place that has to justify it — here.

        Refusals, in the order they are checked:

        - ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` off → 409. Checked *before*
          looking at the row, and it refuses even when the row happens to still
          hold text from when the gate was open. That flag means "stop showing
          me this text"; re-running it to produce a fresh trace is not
          honouring that, and the fresh trace would be write-gated anyway.
        - no stored text → 409. The trace was captured with the gate closed;
          the hash identifies the message but cannot reproduce it.
        - no ``user_id`` → 409. The sender's account is gone, and "route this
          for nobody" is not a question with an answer.

        Returns a detached :class:`ReplaySource`, never the row.
        """
        from app.core.config import settings

        if not settings.ROUTING_TRACE_STORE_MESSAGE_TEXT:
            raise RoutingSimulationError(
                409,
                "Replay needs the original message, and "
                "ROUTING_TRACE_STORE_MESSAGE_TEXT is off. While it is off no "
                "message text is captured, and stored text is not served or "
                "re-run. Use POST /api/v1/admin/routing/simulate with the "
                "message typed in instead.",
            )
        row = db.get(RoutingDecision, trace_id)
        if row is None:
            raise RoutingSimulationError(404, "Routing trace not found")
        if not row.message_text:
            raise RoutingSimulationError(
                409,
                "This trace has no stored message text — it was captured while "
                "ROUTING_TRACE_STORE_MESSAGE_TEXT was off, so only its hash "
                "survives. Use POST /api/v1/admin/routing/simulate with the "
                "message typed in instead.",
            )
        if row.user_id is None:
            raise RoutingSimulationError(
                409,
                "This trace's sender account no longer exists, so there is no "
                "routing state to replay against.",
            )
        # Detached here, while the row is freshly loaded — not by the caller
        # after an intervening commit has expired it.
        return ReplaySource(
            message=row.message_text,
            user_id=row.user_id,
            channel_id=row.channel_id,
            thread_key=row.thread_key,
        )

    @staticmethod
    def diff(
        original: RoutingDecisionPublic, replay: RoutingDecisionPublic
    ) -> RoutingReplayDiff:
        """What changed between a decision and its re-run.

        Computed here rather than in the UI so the wording is testable and
        lives with the rules it describes — the same call plan §9 makes for the
        reachability verdict.

        Candidate sets are compared by ``ref_id`` and reported by name. That
        split matters: the id is what makes the comparison correct across a
        rename, and the name is what makes the answer readable. A candidate
        that appeared or disappeared usually *is* the explanation for an
        outcome change, which is why it gets its own field rather than being
        left for the reader to spot in two long tables.
        """
        original_selection = _selection_label(original)
        replay_selection = _selection_label(replay)
        before = _candidate_names(original.stages)
        after = _candidate_names(replay.stages)

        outcome_changed = original.outcome != replay.outcome
        selection_changed = original_selection != replay_selection
        match_method_changed = original.match_method != replay.match_method
        added = sorted(name for ref, name in after.items() if ref not in before)
        removed = sorted(name for ref, name in before.items() if ref not in after)

        diff = RoutingReplayDiff(
            changed=bool(
                outcome_changed or selection_changed or match_method_changed
                or added or removed
            ),
            outcome_changed=outcome_changed,
            original_outcome=original.outcome,
            replay_outcome=replay.outcome,
            selection_changed=selection_changed,
            original_selection=original_selection,
            replay_selection=replay_selection,
            match_method_changed=match_method_changed,
            original_match_method=original.match_method,
            replay_match_method=replay.match_method,
            original_confidence=original.confidence,
            replay_confidence=replay.confidence,
            original_candidate_count=original.candidate_count,
            replay_candidate_count=replay.candidate_count,
            candidates_added=added,
            candidates_removed=removed,
        )
        diff.summary = _diff_summary(diff)
        return diff

    # ── Recommendation draft ─────────────────────────────────────────

    @staticmethod
    def recommend(
        db: DBSession,
        *,
        trace: RoutingDecisionPublic,
        message: str,
        ref_id: str | None,
        actor: Any,
    ) -> RoutingRecommendationPublic:
        """Draft a better trigger prompt for one candidate. **Writes nothing.**

        Not "writes nothing to the trace" — writes nothing at all. It reads a
        candidate off the trace, asks
        :meth:`AIFunctionsService.generate_router_trigger_prompt` for wording
        that would have matched this message, and returns it for the admin to
        send to the agent's owner. The owner applies it, or does not. That is
        the boundary in plan §1 and it is why the response carries
        ``RECOMMENDATION_ADVISORY_NOTICE``.

        ``ref_id`` must name a candidate **of this trace**. Without that
        restriction the route would be a general "draft a trigger prompt for
        any agent id" oracle that happens to hang off a diagnostics endpoint,
        which is a different and much wider capability than the one being
        added.

        A generator failure comes back as ``success=False`` with the error
        rather than as an exception: the card's other job is answering "is my
        local LLM broken", so a provider outage here is itself a diagnosis.
        """
        from app.services.ai_functions.ai_functions_service import AIFunctionsService

        candidate = _pick_candidate(trace, ref_id)
        name = str(candidate.get("name") or "").strip() or "this agent"
        current = candidate.get("trigger_prompt") or None
        examples = candidate.get("prompt_examples") or None

        description = _recommendation_description(
            name=name, current=current, examples=examples, message=message
        )
        try:
            generated = AIFunctionsService.generate_router_trigger_prompt(
                agent_name=name, description=description, user=actor, db=db
            )
        except Exception as exc:  # noqa: BLE001 — an advisory draft must not 500
            logger.warning("Routing recommendation draft failed", exc_info=True)
            generated = {"success": False, "error": routing_trace.describe_exception(exc)}

        return RoutingRecommendationPublic(
            trace_id=trace.id,
            ref_id=str(candidate.get("ref_id") or ""),
            kind=str(candidate.get("kind") or ""),
            name=name,
            owner_email=candidate.get("owner_email"),
            current_trigger_prompt=current,
            suggested_trigger_prompt=generated.get("trigger_prompt"),
            success=bool(generated.get("success")),
            error=generated.get("error"),
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _selection_label(decision: RoutingDecisionPublic) -> str | None:
    """``"agent:<name>"`` / ``"bundle:<name>"``, or ``None`` for no selection.

    Names rather than ids because the label is read by a person; prefixed by
    kind because an agent and the bundle it came from can share a display name
    and "changed from X to X" would be the least useful diff line possible.
    """
    if decision.selected_agent_id is not None:
        return f"agent:{decision.selected_agent_name or decision.selected_agent_id}"
    if decision.selected_bundle_uuid is not None:
        return f"bundle:{decision.selected_bundle_name or decision.selected_bundle_uuid}"
    return None


def _candidate_names(stages: Any) -> dict[str, str]:
    """``{ref_id: display name}`` across every stage.

    Defensive throughout, like the other ``stages`` readers: this is JSONB
    written by a recorder whose dataclasses will keep changing, and a row
    written by an older build must not 500 a diff.
    """
    found: dict[str, str] = {}
    try:
        for stage in stages or []:
            for candidate in (stage or {}).get("candidates") or []:
                candidate = candidate or {}
                ref = candidate.get("ref_id")
                if not ref:
                    continue
                found[str(ref)] = str(candidate.get("name") or ref)
    except Exception:  # noqa: BLE001
        logger.debug("Routing replay candidate extraction failed", exc_info=True)
    return found


def _diff_summary(diff: RoutingReplayDiff) -> str:
    """One plain-language sentence for the replay's before/after.

    Says "nothing changed" out loud rather than rendering an empty diff. An
    unchanged replay is a real and useful answer — it means the change the
    admin just made did *not* fix the routing — and an empty panel would read
    as though the replay had not run. §11a Rule 1 on a read surface.
    """
    parts: list[str] = []
    if diff.outcome_changed:
        parts.append(f"outcome {diff.original_outcome} → {diff.replay_outcome}")
    if diff.selection_changed:
        parts.append(
            f"selection {diff.original_selection or 'none'} → "
            f"{diff.replay_selection or 'none'}"
        )
    if diff.match_method_changed:
        parts.append(
            f"match method {diff.original_match_method or 'none'} → "
            f"{diff.replay_match_method or 'none'}"
        )
    if diff.candidates_added:
        parts.append("newly considered: " + ", ".join(diff.candidates_added))
    if diff.candidates_removed:
        parts.append("no longer considered: " + ", ".join(diff.candidates_removed))
    if not parts:
        return (
            "Nothing changed — this message routes the same way now as it did "
            "then, over the same candidates."
        )
    return "; ".join(parts)


def _pick_candidate(trace: RoutingDecisionPublic, ref_id: str | None) -> dict:
    """The candidate to draft for, or a refusal naming what to pass.

    With ``ref_id`` given it must match a candidate of this trace. Without one,
    the server picks only when the choice is unambiguous — the decision's own
    selection, or a sole candidate. Anything else asks rather than guessing: a
    silently-picked wrong subject produces a plausible draft for the wrong
    agent, which is worse than an error because it looks like an answer.
    """
    candidates: list[dict] = []
    try:
        for stage in trace.stages or []:
            for candidate in (stage or {}).get("candidates") or []:
                if isinstance(candidate, dict) and candidate.get("ref_id"):
                    candidates.append(candidate)
    except Exception:  # noqa: BLE001
        logger.debug("Routing recommendation candidate scan failed", exc_info=True)

    if not candidates:
        raise RoutingSimulationError(
            409,
            "This trace considered no candidates, so there is nothing to draft "
            "a trigger prompt for. The reachability problem is upstream: the "
            "agent you expected was never a routing candidate at all.",
        )

    if ref_id:
        for candidate in candidates:
            if str(candidate.get("ref_id")) == ref_id:
                return candidate
        raise RoutingSimulationError(
            404,
            f"No candidate {ref_id} in this trace. Pass a ref_id from the "
            f"trace's stages[].candidates[].",
        )

    selected = trace.selected_agent_id or trace.selected_bundle_uuid
    if selected is not None:
        for candidate in candidates:
            if str(candidate.get("ref_id")) == str(selected):
                return candidate
    if len(candidates) == 1:
        return candidates[0]
    raise RoutingSimulationError(
        400,
        f"This trace considered {len(candidates)} candidates and picked none, "
        f"so there is no obvious subject for a recommendation. Pass ref_id to "
        f"say which candidate to draft for.",
    )


def _recommendation_description(
    *, name: str, current: str | None, examples: str | None, message: str
) -> str:
    """The brief handed to the trigger-prompt generator.

    ``generate_router_trigger_prompt`` takes a name and a free-text description
    of what the agent does; this assembles one from what the trace knows — the
    owner's current routing configuration plus the message that failed to reach
    it. The failed message is the whole input that makes this better than
    re-running the generator on the agent's description alone.
    """
    parts = [f"Agent: {name}."]
    if current:
        parts.append(f"Its current router trigger prompt is: {current}")
    if examples:
        parts.append(f"Its current example messages are: {examples}")
    parts.append(
        "A user sent the following message and the router did NOT route it to "
        f"this agent, although it should have: {message}"
    )
    parts.append(
        "Write a replacement router trigger prompt that would match that "
        "message while keeping the agent's existing scope."
    )
    return "\n".join(parts)
