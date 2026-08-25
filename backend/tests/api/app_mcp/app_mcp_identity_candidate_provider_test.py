"""``IdentityCandidateProvider`` — parity with the pre-refactor identity arm.

Phase 1 of ``docs/plans/channels_identity_unification/`` extracted identity
out of ``AppAgentRouteService.get_effective_routes_for_user`` into
``app.services.routing.identity_candidate_provider.IdentityCandidateProvider``,
composed by ``AppMCPRoutingService.route_message`` (see that module's
docstring). This file pins the two properties phase_1_identity_routing_layer.md
§5 calls out for the provider itself:

  1. **Candidate collapse** — a caller with two accessible bindings from one
     owner sees exactly ONE candidate, with the owner's name and aggregated
     trigger prompt matching the pre-refactor text verbatim (pinned against
     ``git show HEAD:backend/app/services/app_mcp/app_agent_route_service.py``
     before the refactor landed).
  2. **Skips are recorded, and the boundary is exact** — an owner who named
     this caller on a binding but has every such binding switched off is a
     *skipped* candidate in the trace, not a silently dropped one; an owner
     who never named this caller produces no row at all.

WHY THIS CALLS ``IdentityCandidateProvider.build()`` DIRECTLY
---------------------------------------------------------------
``tests/api/app_mcp/`` has no HTTP route to drive — App MCP is an MCP
tool-call surface, not a REST endpoint, and every existing test in this
directory (see ``app_mcp_session_test.py``'s module docstring: "These tests
call handle_send_message() directly (not through MCP protocol)") already
calls the service layer directly as this domain's entry point. This file
follows that same, already-established convention one layer further down:
``IdentityCandidateProvider.build()`` is the unit phase 1 introduced and the
one the plan's test list names explicitly, and going through
``AppMCPRoutingService.route_message()`` would force either an AI-classifier
stub (item 1 wants to pin the candidate's *name*/*trigger_prompt* text, not a
routing verdict) or contorting the ballot down to exactly one candidate to hit
the ``only_one`` shortcut, which is a weaker assertion of the same fact this
test states directly.

Every row the provider reads (agent, binding, assignment) is created through
the identity/agent APIs — only the function call under test bypasses HTTP,
exactly as the rest of this domain does.
"""
import dataclasses
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.routing import routing_trace
from app.services.routing.identity_candidate_provider import (
    IdentityCandidateProvider,
    identity_ref_id,
)
from app.services.server_channels.channel_policy_service import ResolvedChannelPolicy
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.identity import (
    create_identity_binding,
    toggle_identity_contact,
    update_identity_binding,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer


CONSENTING_POLICY = dataclasses.replace(
    ResolvedChannelPolicy.for_no_channel(), allow_identity_routing=True
)
"""The channel-less policy, with the sender's identity opt-in switched on.

Both tests below ask the provider a question about *its own* mechanics — how
two bindings collapse into one candidate, and where the line between a skip row
and no row at all falls — none of which is about consent. They used to reach
that state by omitting ``policy=`` entirely and taking the permissive default;
phase 7 removed that default (a gate that can route into somebody else's
workspace does not get a permissive one), so the precondition those questions
always assumed is now stated rather than inherited. ``for_no_channel()`` is the
real policy for a decision belonging to no channel, and its
``allow_identity_routing=False`` is the only field these tests are not asking
about — hence the ``replace``. Nothing else about either test changed.
"""


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# 1. Candidate collapse + verbatim text parity
# ---------------------------------------------------------------------------


def test_two_bindings_from_one_owner_collapse_into_one_candidate_with_pinned_text(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    A caller with two accessible bindings from the SAME owner sees exactly one
    candidate on the identity ballot:

      1. Owner (superuser) creates two agents, each with its own identity
         binding, both naming the same caller — assignments enabled by the
         caller opting in (the recipient-side toggle, not auto_enable).
      2. ``IdentityCandidateProvider.build()`` returns exactly one Candidate.
      3. ``Candidate.ref_id`` is the namespaced owner ref
         (``identity:{owner_id}``), never the placeholder or a bare agent id.
      4. ``Candidate.name`` == ``owner.full_name or owner.email`` — pinned
         against the pre-refactor arm's fallback chain.
      5. ``Candidate.trigger_prompt`` == the exact pre-refactor sentence:
         ``"Contact {full_name or email} ({email}). Routes to their
         available agents."``
      6. ``Candidate.prompt_examples`` aggregates both bindings' examples,
         each re-voiced as "ask {name} ({email}) to {line}" — asserted as a
         SET (binding insertion order is by binding UUID, not creation
         order, so line order is not a stable thing to pin).
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])

    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent_1 = create_agent_via_api(client, superuser_token_headers, name="Identity Agent One")
    agent_2 = create_agent_via_api(client, superuser_token_headers, name="Identity Agent Two")

    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent_1["id"],
        trigger_prompt="Handle billing questions.",
        prompt_examples="book a demo\nreschedule a demo",
        assigned_user_ids=[caller["id"]],
    )
    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent_2["id"],
        trigger_prompt="Handle support questions.",
        prompt_examples="cancel a subscription",
        assigned_user_ids=[caller["id"]],
    )

    # The caller opts in to both — the recipient-side toggle enables ALL of
    # this owner's assignments to them at once (per-person, not per-binding).
    toggle_identity_contact(client, caller_headers, owner["id"], is_enabled=True)

    candidates = IdentityCandidateProvider.build(
        db, caller_id, policy=CONSENTING_POLICY
    )

    assert len(candidates) == 1, (
        f"Two bindings from one owner must collapse into ONE candidate, got "
        f"{len(candidates)}: {[c.ref_id for c in candidates]}"
    )
    candidate = candidates[0]

    # ── ref_id is namespaced ───────────────────────────────────────────────
    assert candidate.ref_id == identity_ref_id(owner_id)
    assert candidate.ref_id == f"identity:{owner_id}"

    # ── name: full_name or email or "" (pre-refactor fallback chain) ──────
    expected_name = owner["full_name"] or owner["email"] or ""
    assert candidate.name == expected_name

    # ── trigger_prompt: pinned verbatim ────────────────────────────────────
    expected_trigger = (
        f"Contact {owner['full_name'] or owner['email']} ({owner['email']}). "
        f"Routes to their available agents."
    )
    assert candidate.trigger_prompt == expected_trigger

    # ── prompt_examples: aggregated across both bindings, order-independent ─
    owner_name_for_examples = owner["full_name"] or ""
    owner_email = owner["email"] or ""
    expected_lines = {
        f"ask {owner_name_for_examples} ({owner_email}) to book a demo",
        f"ask {owner_name_for_examples} ({owner_email}) to reschedule a demo",
        f"ask {owner_name_for_examples} ({owner_email}) to cancel a subscription",
    }
    assert candidate.prompt_examples is not None
    assert set(candidate.prompt_examples.splitlines()) == expected_lines


# ---------------------------------------------------------------------------
# 2. Skip recording — both sides of the boundary
# ---------------------------------------------------------------------------


def test_owner_with_only_inactive_bindings_is_a_recorded_skip_not_a_drop(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Two identity owners exist; only one ever named this caller:

      - Owner A named the caller on a binding, then switched the binding off
        (``is_active=False``). Per plan §2.1 this must appear in the routing
        trace as a SKIPPED candidate (``SKIP_IDENTITY_UNAVAILABLE``) — not
        silently absent, so a trace reader can tell "the expected candidate
        was never on the ballot at all" apart from "nobody ever configured
        this contact".
      - Owner B has an active binding and an active/enabled assignment, but
        to a DIFFERENT user — never to this caller. Owner B must produce NO
        row at all for this caller: a list of every identity owner on the
        platform is not a diagnosis (module docstring of
        ``identity_candidate_provider.py``).

    Both facts are asserted in the same test so the boundary is checked on
    both sides at once, not just the "skip exists" half.
    """
    owner_a, owner_a_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner_a["id"])
    create_random_ai_credential(client, owner_a_headers, set_default=True)
    agent_a = create_agent_via_api(client, owner_a_headers, name="Owner A Agent")

    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    binding_a = create_identity_binding(
        client,
        owner_a_headers,
        agent_id=agent_a["id"],
        trigger_prompt="Owner A's agent.",
        assigned_user_ids=[caller["id"]],
    )
    toggle_identity_contact(client, caller_headers, owner_a["id"], is_enabled=True)
    # Owner A switches the binding off — this is the only binding they have,
    # so the owner is now unreachable, not merely one binding down.
    update_identity_binding(client, owner_a_headers, binding_a["id"], is_active=False)

    # Owner B: fully live binding/assignment, but never to THIS caller.
    owner_b, owner_b_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner_b["id"])
    create_random_ai_credential(client, owner_b_headers, set_default=True)
    agent_b = create_agent_via_api(client, owner_b_headers, name="Owner B Agent")
    other_target, other_target_headers = create_random_user_with_headers(client)
    create_identity_binding(
        client,
        owner_b_headers,
        agent_id=agent_b["id"],
        trigger_prompt="Owner B's agent.",
        assigned_user_ids=[other_target["id"]],
    )
    toggle_identity_contact(client, other_target_headers, owner_b["id"], is_enabled=True)

    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_APP_MCP, message="who can help me?"
    ) as trace:
        candidates = IdentityCandidateProvider.build(
            db, caller_id, policy=CONSENTING_POLICY
        )

    # ── Returned candidates: neither owner appears ─────────────────────────
    # Owner A is skipped (excluded, not returned); Owner B never matched the
    # caller in the query at all.
    assert candidates == []

    # ── Trace: Owner A recorded as a skip, Owner B has no row whatsoever ───
    assert len(trace.stages) == 1
    recorded = trace.stages[0].candidates
    owner_a_id = uuid.UUID(owner_a["id"])
    owner_b_id = uuid.UUID(owner_b["id"])

    owner_a_rows = [c for c in recorded if c.ref_id == identity_ref_id(owner_a_id)]
    owner_b_rows = [c for c in recorded if c.ref_id == identity_ref_id(owner_b_id)]

    assert len(owner_a_rows) == 1, (
        f"Owner A (bindings all inactive) must be recorded exactly once as a "
        f"skip, got {len(owner_a_rows)} rows"
    )
    skip_row = owner_a_rows[0]
    assert skip_row.eligible is False
    assert skip_row.skip_reason == routing_trace.SKIP_IDENTITY_UNAVAILABLE

    assert owner_b_rows == [], (
        "Owner B never named this caller and must produce NO row at all — "
        "a skip would be as misleading here as a silent drop, since nobody "
        "ever expected to reach Owner B"
    )
