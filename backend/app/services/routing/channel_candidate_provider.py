"""The candidate set for Server Channel routing: the sender's own agents.

Each routing surface owns its **candidate provider**, and this is the one for
Server Channels. The only things legitimately shared across surfaces are
``AgentClassifier.classify`` and ``RoutingTrace`` — never the candidate set,
and never another surface's enablement toggles.

**Why this module exists.** Channel Pass 1 used to call
``AppMCPRoutingService.route_message``, whose candidates came from the deleted
``AppAgentRouteService.get_effective_routes_for_user``. That function answered
*"what can this user address through the App MCP server"* — admin-created
``AppAgentRoute`` rows assigned to them, their ``UserAppAgentRoute`` rows, and
identity contacts — which is a different question from *"which agents does this
user own"* in both directions:

- A standalone agent (``bundle_uuid IS NULL``) never gets an auto-route, so an
  owner's own agent could be absent from the set entirely. That is the reported
  bug: the sender owned exactly one agent, it had a trigger prompt, and it was
  not on the ballot.
- An admin route assigned to the user, or an identity contact, resolves to
  **somebody else's** agent. Those could never produce a usable channel session
  (``ChannelIngestionService.assert_access`` forbids a ``channel_caller``
  session on a foreign agent), but they could and did win the classification
  and take the decision with them.

A channel is always used by the owner themself, from outside the platform, so
the right base set is constructed here rather than filtered down from the wrong
one afterwards. Nothing in this module reads ``AppAgentRoute``,
``AppAgentRouteAssignment``, ``UserAppAgentRoute``, ``IdentityAgentBinding`` or
``channel_app_mcp``: what a user chooses to expose over MCP is not a statement
about what they can reach from their own chat app, and inheriting those
switches made an MCP toggle silently unreachable-ify an owner's own agent.

**Eligibility** is two questions, asked in this order: is the agent *in scope*
for this channel, and does it have anything to classify on?

**Scope** comes from the resolved :class:`ResolvedChannelPolicy` the caller
hands in — never from a lookup this module makes. Every inherit rule lives in
``ChannelPolicyService`` and this module holds none of them; what arrives here
is an already-answered ``agent_scope`` of ``"all"`` / ``"list"`` / ``"none"``
plus, for ``"list"``, the ids. The policy is a frozen dataclass rather than a
``ServerChannel`` row for the reason its own docstring gives: this runs inside
``ChannelRoutingService.decide``'s worker threads, and an ORM row crossing that
boundary turns the next attribute read into a lazy reload against a closed
session.

**Wording** is the second question and is unchanged: a non-blank
``router_trigger_prompt``, or a non-empty ``example_prompts``.

``Agent.example_prompts`` is the agent-level SSOT this reads, and it already
existed — it is snapshotted onto every bundle revision at publish time,
restored on install, tracked as a drift field by the git source service, and
edited by the owner in the UI. Note the near-miss:
``AppAgentRoute.prompt_examples`` is a *newline-separated ``str`` on a
different model* for admin-authored routes, and ``Candidate.prompt_examples``
is that same ``str`` shape. The list is joined into it at the one call site
below; there is deliberately no shared "forward" helper, because a third
representation of prompt examples is the trap here, not the one ``join``.

**Nothing is dropped silently.** An agent with neither wording field is
recorded into the trace as a skipped candidate (``SKIP_NO_TRIGGER_PROMPT``);
an agent outside the channel's scope is recorded as one too
(``SKIP_NOT_IN_CHANNEL_SCOPE``). A candidate list showing only the finalists
cannot explain the failure that actually bites — *the expected agent was never
a candidate at all* — which is the whole reason the incident above needed a
database query to diagnose. Scope makes that failure mode cheap to reach: with
``agent_scope="none"`` **every** agent the sender owns is out, and the trace
has to be able to say so rather than come back empty (master plan §3.5).

See ``docs/application/server_channels/server_channels.md`` for why a channel
routes over the agents the sender owns, and
``docs/application/server_channels/server_channels_tech.md`` for this
provider's contract.
"""
from __future__ import annotations

import logging
import uuid

from sqlmodel import Session as DBSession, select

from app.models import (
    CHANNEL_AGENT_SCOPE_ALL,
    CHANNEL_AGENT_SCOPE_LIST,
    Agent,
)
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import Candidate
from app.services.server_channels.channel_policy_service import ResolvedChannelPolicy

logger = logging.getLogger(__name__)

#: ``CandidateTrace.source`` for everything this provider records. The App MCP
#: sources ("admin" / "user" / "identity") all name *where the route came
#: from*; a channel candidate has no route, so it names the relationship that
#: actually made it eligible.
SOURCE_OWNED = "owned"


def example_prompt_text(raw: object) -> str | None:
    """``Agent.example_prompts`` as the ``str`` a ``Candidate`` carries.

    ``example_prompts`` is a JSON column with no validator on the way in
    (``AgentService.update_agent`` passes it through), so this treats it as
    untrusted shape rather than as ``list[str]``: a non-list value yields no
    examples instead of iterating a string into characters, and blank entries
    are dropped so a ``[""]`` cannot make an agent "eligible" with nothing to
    classify on.

    Length is **not** bounded here. ``agent_classifier`` re-applies the
    2000-character / 10-line limit at render time, which is the one place that
    covers every source — including rows already in the database, which no
    write-time validator can reach.
    """
    if not isinstance(raw, list):
        return None
    lines = [str(item).strip() for item in raw]
    lines = [line for line in lines if line]
    return "\n".join(lines) if lines else None


class ChannelCandidateProvider:
    """Builds the Pass-1 candidate list from the agents the sender owns."""

    @staticmethod
    def build(
        db: DBSession, user_id: uuid.UUID, *, policy: ResolvedChannelPolicy
    ) -> list[Candidate]:
        """Every eligible agent owned by ``user_id``, recorded onto the trace.

        Ordered by name so one routing state renders one prompt: the classifier
        sees candidates in a stable order, and a trace can be compared with its
        own replay without the database's return order being a hidden variable.

        ``policy`` is **required and keyword-only**. It has no default, and that
        is deliberate rather than strict for its own sake: a default would have
        to be a permissive one, and a permissive default on a candidate builder
        is a scope restriction that silently stops applying the day somebody
        adds a call site and does not read this docstring. Every caller has a
        policy — the webhook resolves the sender's, simulate resolves the
        replayed channel's or explicitly says there is no channel (see
        ``ResolvedChannelPolicy.for_no_channel``) — so requiring it costs
        nothing and removes the failure mode.

        The query is unchanged and still ``WHERE owner_id = :user_id``: scope
        narrows what may be *classified*, never what may be *seen*. Filtering in
        SQL would be cheaper and is the wrong shape — an agent excluded by the
        ``WHERE`` clause cannot be recorded as a skip, and a skip that is never
        recorded is the one failure this provider exists to make diagnosable.
        A sender owns a handful of agents, so the cost of loading the ones that
        will be skipped is the cost of being able to explain them.
        """
        agents = db.exec(
            select(Agent)
            .where(Agent.owner_id == user_id)
            .order_by(Agent.name, Agent.id)
        ).all()

        candidates: list[Candidate] = []
        skipped_out_of_scope = 0
        for agent in agents:
            # Read every attribute once, up front, off the instance the query
            # just materialised. Passing ``agent.name`` inline into a recorder
            # is safe only as long as nothing in this loop commits — the same
            # "safe by coincidence" shape swept out of Pass 2, where a lazy
            # reload would raise *before* the recorder's own guard is entered.
            agent_id = agent.id
            agent_name = agent.name or ""
            trigger = (agent.router_trigger_prompt or "").strip()
            examples = example_prompt_text(agent.example_prompts)

            # --- Scope first, and it wins over the wording check below. ---
            #
            # An agent can fail both tests at once, and it gets one row with one
            # reason, so the two orderings are a real choice about what the
            # sender is told. Scope wins for three reasons, the last decisive:
            #
            # - It is channel-specific and it is what the sender was just
            #   editing. "Not switched on for this channel" is answerable in the
            #   screen they came from.
            # - An out-of-scope agent's wording is nobody's business on this
            #   channel. Reporting it would be reporting a fact about a
            #   candidate that was never in contention.
            # - Reporting ``no_trigger_prompt`` here would be a confidently
            #   wrong diagnosis: the reader goes and sets a trigger prompt, the
            #   agent still does not route, and nothing anywhere told them why.
            #   A coarse answer is survivable; a wrong one that costs a round
            #   trip and ends where it started is not.
            #
            # The skip carries the wording anyway, because the near-miss ranking
            # scores excluded candidates too — "the agent you excluded is the
            # one that would have matched" is a strictly better answer than
            # "excluded", and it is only available if these fields travel.
            if not ChannelCandidateProvider._in_scope(agent_id, policy):
                skipped_out_of_scope += 1
                routing_trace.record_skip(
                    kind=routing_trace.KIND_AGENT,
                    ref_id=agent_id,
                    name=agent_name,
                    reason=routing_trace.SKIP_NOT_IN_CHANNEL_SCOPE,
                    source=SOURCE_OWNED,
                    trigger_prompt=trigger,
                    prompt_examples=examples,
                )
                continue

            if not trigger and not examples:
                routing_trace.record_skip(
                    kind=routing_trace.KIND_AGENT,
                    ref_id=agent_id,
                    name=agent_name,
                    reason=routing_trace.SKIP_NO_TRIGGER_PROMPT,
                    source=SOURCE_OWNED,
                )
                continue

            routing_trace.record_candidate(
                kind=routing_trace.KIND_AGENT,
                ref_id=agent_id,
                name=agent_name,
                source=SOURCE_OWNED,
                trigger_prompt=trigger,
                prompt_examples=examples,
            )
            candidates.append(
                Candidate(
                    ref_id=str(agent_id),
                    name=agent_name,
                    trigger_prompt=trigger,
                    prompt_examples=examples,
                )
            )

        logger.debug(
            "[ChannelCandidates] user=%s owns %d agents, %d eligible "
            "(scope=%s, %d out of scope)",
            user_id,
            len(agents),
            len(candidates),
            policy.agent_scope,
            skipped_out_of_scope,
        )
        return candidates

    @staticmethod
    def _in_scope(agent_id: uuid.UUID, policy: ResolvedChannelPolicy) -> bool:
        """Is this owned agent one the sender enabled for this channel?

        Written as an allowlist — only the two scopes that admit anything say
        yes — so an ``agent_scope`` this code has never met admits nothing.
        That direction is the same one ``ChannelPolicyService`` degrades in and
        for the same reason: nothing routing is the *visible* failure, because
        every owned agent lands in the trace with a reason, whereas everything
        routing would be an over-broad ballot that looks like it worked.

        ``"list"`` reads ``allowed_agent_ids``, and an empty (or ``None``) set
        there means the list is the mechanism and it is empty — which admits
        nothing, and is a different state from ``"all"`` even though both are
        spelled with the same field. See ``ResolvedChannelPolicy``.
        """
        if policy.agent_scope == CHANNEL_AGENT_SCOPE_ALL:
            return True
        if policy.agent_scope == CHANNEL_AGENT_SCOPE_LIST:
            return agent_id in (policy.allowed_agent_ids or frozenset())
        return False
