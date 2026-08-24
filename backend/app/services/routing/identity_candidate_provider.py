"""The candidate set contributed by *identities*: people, as pseudo-agents.

Sibling of ``channel_candidate_provider``. Each routing surface owns its
candidate provider, and providers **compose** — the App MCP surface's ballot is
"the routes this user can address" *plus* "the people this user can address",
and a surface that wants only one of those simply does not call the other.
That composition is the point: identity used to be an arm buried inside
``AppAgentRouteService.get_effective_routes_for_user``, reachable only from
App MCP Stage 1, so no other surface could offer it without inheriting three
App MCP enablement toggles along with it.

**One candidate per identity owner, not per binding.** A caller who can reach
four of somebody's agents still sees one person on the ballot; picking that
person is Stage 1's whole job, and Stage 2
(``IdentityRoutingService.route_within_identity``) picks the agent afterwards.
Two stages is also the recursion cap — an identity cannot route to an identity
— and that is the intended guard, not an omission.

**``ref_id`` is namespaced, and that is load-bearing.**
``Candidate.ref_id`` is a plain ``str`` and every other provider puts an agent
(or bundle) UUID in it; consumers routinely do ``uuid.UUID(result.agent_id)``
and look the result up as an agent. An identity candidate names a *person*, so
a bare owner UUID here would be indistinguishable from an agent id and would be
looked up as one. It is therefore ``identity:{owner_id}``, built by
:func:`identity_ref_id` and read back by :func:`parse_identity_ref` — one
place, so no consumer has to know the prefix spelling.

The pre-refactor code had the failure this namespacing prevents, in its milder
form: every identity route carried the *same* placeholder ``agent_id``
(``UUID(int=0)``), so two identity owners on one ballot rendered two candidates
with one id and the second was unreachable. Namespacing fixes that as a side
effect; it is not the reason for it.

**Nothing is dropped silently.** An owner who named this caller on a binding
but whose bindings or assignments are switched off is recorded as a skipped
candidate (``SKIP_IDENTITY_UNAVAILABLE``), not omitted — master plan §3.5: a
candidate excluded without a ``skip_reason`` cannot diagnose the failure that
actually bites, *the expected candidate was never on the ballot at all*. The
boundary is deliberate: an owner who never named this caller produces no row
here, because nobody expected to reach them and a list of every identity owner
on the platform is not a diagnosis.

Those rows are **visible on the channel path and nowhere else.** Since Phase 3
of the channels & identity unification ``ChannelRoutingService._route_installed``
calls this provider inside Pass 1's ``RoutingTrace.capture()``, so a channel
decision carries both the eligible identity candidates and the
``SKIP_IDENTITY_UNAVAILABLE`` rows (the reachability verdict has a channel-voiced
explanation for that reason, added in the same change). The App MCP request
handler still opens no capture, so on that surface every recorder call here is
still a no-op — which is what the skips being written before a capture existed
bought: the instrumentation was already correct on the day a capture appeared,
rather than being added after somebody needed it.

**One thing is deliberately NOT recorded**, and it is the feature's single
inversion of master plan §3.5: when the sender has not switched identity
routing on for the channel, ``ChannelRoutingService`` does not call this
provider at all, so the identity owners they *could* have reached leave no rows
— not even skips. Recording them would publish the existence of other people's
identities into a trace an external sender can trigger at will. The reasoning
is at the call site, which is where somebody would otherwise "fix" it.

**Ordering is fixed here, and it was not before.** The arm this replaces ran
its owner query and its per-owner example query with no ``ORDER BY`` at all, so
the candidate block order and the aggregated example-line order were whatever
the database returned. Both are prompt inputs, so both were an unpinned
variable in every routing decision with two identity owners (or one owner with
two reachable bindings). Sorting them is a deliberate change in *ordering*, not
in text — the same one ``ChannelCandidateProvider`` makes, for the same reason:
one routing state should render one prompt, so a trace can be compared with its
own replay.

The sort key is the *rendered* name, not the raw columns, so "ordered by name"
means what a reader of the prompt sees. ``ChannelCandidateProvider`` gets that
for free — its ``Agent.name`` column *is* the rendered name — but here the
render is ``owner.full_name or owner.email or ""``, a falsy fallback, so the
key has to reproduce it in SQL:
``coalesce(nullif(full_name, ''), email)``. The ``nullif`` is not decoration.
``full_name`` is nullable *and* unconstrained against the empty string (no
``min_length``, no validator, no normalisation anywhere on the write paths), so
both ``NULL`` and ``''`` reach the database and both render as the email. Plain
``coalesce`` would catch only the ``NULL`` half and leave ``''`` sorting ahead
of every real name — do not "simplify" it back.

See ``docs/plans/channels_identity_unification/phase_1_identity_routing_layer.md`` §2.1.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func
from sqlmodel import Session as DBSession, select

from app.models import User
from app.models.identity.identity_models import (
    IdentityAgentBinding,
    IdentityBindingAssignment,
)
from app.services.routing import routing_trace
from app.services.routing.agent_classifier import Candidate

logger = logging.getLogger(__name__)

#: ``CandidateTrace.source`` for everything this provider records. Matches the
#: string the identity arm of ``get_effective_routes_for_user`` used, so a
#: trace written before this refactor and one written after read the same.
SOURCE_IDENTITY = "identity"

#: The ``ref_id`` namespace. Not a UUID, on purpose — see the module docstring.
IDENTITY_REF_PREFIX = "identity:"


def identity_ref_id(owner_id: uuid.UUID) -> str:
    """The ``Candidate.ref_id`` naming an identity owner."""
    return f"{IDENTITY_REF_PREFIX}{owner_id}"


def parse_identity_ref(ref_id: str) -> uuid.UUID | None:
    """The owner id behind an identity ``ref_id``, or ``None``.

    ``None`` for anything that is not an identity ref — including a namespaced
    ref whose tail is not a UUID, which is a corrupt value rather than an agent
    id and must not be handed on as one.
    """
    if not isinstance(ref_id, str) or not ref_id.startswith(IDENTITY_REF_PREFIX):
        return None
    try:
        return uuid.UUID(ref_id[len(IDENTITY_REF_PREFIX):])
    except ValueError:
        logger.warning("[IdentityCandidates] Malformed identity ref_id: %r", ref_id)
        return None


def _contact_trigger_prompt(owner: User) -> str:
    """The one-line description the classifier sees for a person.

    Reproduced verbatim from the identity arm this provider replaced. The
    wording is not decoration: it is what the model reads, so an edit here is a
    routing-behaviour change and belongs in a change that says so.
    """
    return (
        f"Contact {owner.full_name or owner.email} ({owner.email}). "
        f"Routes to their available agents."
    )


def _contact_examples(owner: User, bindings: list[IdentityAgentBinding]) -> str | None:
    """The owner's binding examples, re-voiced as things to ask *them*.

    ``IdentityAgentBinding.prompt_examples`` is written from the agent's point
    of view ("book me a slot"); on this ballot the reader is choosing a person,
    so each line becomes "ask {owner} ({email}) to {line}". Aggregated across
    every binding the caller can reach, because the candidate is the person.

    The name/email fallbacks are the pre-refactor ones and differ from
    :func:`_contact_trigger_prompt`'s on purpose — the name here falls back to
    empty rather than to the email, so a nameless owner renders
    "ask  (a@b.c) to …" exactly as it did before.
    """
    owner_name = owner.full_name or ""
    owner_email = owner.email or ""
    lines: list[str] = []
    for binding in bindings:
        if not binding.prompt_examples:
            continue
        for raw_line in binding.prompt_examples.splitlines():
            line = raw_line.strip()
            if line:
                lines.append(f"ask {owner_name} ({owner_email}) to {line}")
    return "\n".join(lines) if lines else None


class IdentityCandidateProvider:
    """Builds the identity half of a ballot: one candidate per person."""

    @staticmethod
    def build(db: DBSession, caller_user_id: uuid.UUID) -> list[Candidate]:
        """Every identity owner ``caller_user_id`` can currently address.

        One query, not one per owner: the join below returns every
        ``(owner, binding, assignment)`` triple naming this caller — switched
        on or off — and the partition into eligible and skipped happens in
        Python. The arm this replaces ran a second aggregation query per owner
        plus a debug loop that re-read every raw assignment row; neither is
        carried over.

        Ordered by name so one routing state renders one prompt: the classifier
        sees candidates in a stable order, and a trace can be compared with its
        own replay without the database's return order being a hidden variable.

        "Name" here is the name the prompt actually renders —
        ``owner.full_name or owner.email or ""`` — reproduced in SQL as
        ``coalesce(nullif(full_name, ''), email)`` so both fallback steps are in
        the key, matching ``ChannelCandidateProvider``'s
        ``.order_by(Agent.name, Agent.id)`` literally rather than only in
        wording. The ``nullif`` covers the empty-string case that plain
        ``coalesce`` would miss; see the module docstring. ``User.id`` is the
        tiebreak, as ``Agent.id`` is there. The trailing
        ``IdentityAgentBinding.id`` has no channel-side analog: it orders the
        example lines *within* one owner, which the channel provider has no
        equivalent of.
        """
        rows = db.exec(
            select(User, IdentityAgentBinding, IdentityBindingAssignment)
            .join(
                IdentityAgentBinding,
                IdentityAgentBinding.owner_id == User.id,
            )
            .join(
                IdentityBindingAssignment,
                IdentityBindingAssignment.binding_id == IdentityAgentBinding.id,
            )
            .where(IdentityBindingAssignment.target_user_id == caller_user_id)
            .order_by(
                func.coalesce(func.nullif(User.full_name, ""), User.email),
                User.id,
                IdentityAgentBinding.id,
            )
        ).all()

        # Read every attribute off the instances the query just materialised,
        # up front. Same discipline as ``ChannelCandidateProvider``: nothing in
        # the loops below commits, and that must stay true by construction
        # rather than by coincidence.
        owners: dict[uuid.UUID, User] = {}
        # Keyed by binding id rather than accumulated into a list: the unique
        # constraint on ``(binding_id, target_user_id)`` means the join cannot
        # repeat a binding for one caller today, but a dict says so in a way a
        # future join change cannot quietly break — and it does not depend on
        # what ``==`` means for a mapped row, which is not this module's
        # business to know.
        accessible: dict[uuid.UUID, dict[uuid.UUID, IdentityAgentBinding]] = {}
        for owner, binding, assignment in rows:
            owners.setdefault(owner.id, owner)
            reachable = (
                binding.is_active and assignment.is_active and assignment.is_enabled
            )
            bucket = accessible.setdefault(owner.id, {})
            if reachable:
                bucket[binding.id] = binding

        candidates: list[Candidate] = []
        for owner_id, owner in owners.items():
            owner_name = owner.full_name or owner.email or ""
            # Insertion order, which the query's ``ORDER BY`` fixed — so the
            # aggregated example lines render in one stable order per state.
            bindings = list((accessible.get(owner_id) or {}).values())

            if not bindings:
                routing_trace.record_skip(
                    kind=routing_trace.KIND_AGENT,
                    ref_id=identity_ref_id(owner_id),
                    name=owner_name,
                    reason=routing_trace.SKIP_IDENTITY_UNAVAILABLE,
                    source=SOURCE_IDENTITY,
                    owner_email=owner.email,
                    trigger_prompt=_contact_trigger_prompt(owner),
                )
                continue

            trigger = _contact_trigger_prompt(owner)
            examples = _contact_examples(owner, bindings)

            routing_trace.record_candidate(
                kind=routing_trace.KIND_AGENT,
                ref_id=identity_ref_id(owner_id),
                name=owner_name,
                source=SOURCE_IDENTITY,
                owner_email=owner.email,
                trigger_prompt=trigger,
                prompt_examples=examples,
            )
            candidates.append(
                Candidate(
                    ref_id=identity_ref_id(owner_id),
                    name=owner_name,
                    trigger_prompt=trigger,
                    prompt_examples=examples,
                )
            )

        logger.debug(
            "[IdentityCandidates] caller=%s sees %d identity owner(s), %d reachable",
            caller_user_id,
            len(owners),
            len(candidates),
        )
        return candidates


__all__ = [
    "IdentityCandidateProvider",
    "IDENTITY_REF_PREFIX",
    "SOURCE_IDENTITY",
    "identity_ref_id",
    "parse_identity_ref",
]
