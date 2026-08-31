"""``ChannelIngestionService.assert_access`` — the identity-grant escape hatch.

Phase 1 of ``docs/plans/channels_identity_unification/`` (§2.4) adds
``ChannelAccessPolicy.identity_grant``: the one deliberate exception to the
``channel_caller`` three-way owner invariant (agent.owner_id ==
policy.expected_owner_id == sender.platform_user_id). A grant lets a caller
hold a session on somebody ELSE's agent — which is the entire point of
identity — but only after ``assert_access`` re-reads and re-verifies all six
facts behind it against the database (``IdentityService.verify_identity_access``).
This file pins that re-verification: it must accept a genuinely live grant,
and it must reject each of the six conditions individually, plus the
time-of-check/time-of-use case that is the whole reason the check re-reads
rather than trusts.

DIRECT-SERVICE EXEMPTION — READ BEFORE EDITING
------------------------------------------------
``tests/README.md`` Rule 1 prefers API-driven tests. This file cannot be one,
for a structural reason the phase 1 plan names explicitly (§2.3, §2.5): the
grant arm of ``assert_access`` is consulted **only** on the ``channel_caller``
sender-kind arm, and ``ChannelIngestionService.create_identity_session``
currently allowlists **``mcp_caller`` only**
(``_IDENTITY_SESSION_SENDER_KINDS`` in ``channel_ingestion_service.py``) —
lifting that allowlist to accept a ``channel_caller`` sender is explicitly
Phase 3 work, gated on channels carrying identity candidates at all. So there
is today no HTTP route, and no other service entry point, that drives a
``channel_caller`` sender with an ``identity_grant`` through
``assert_access`` — the six-condition re-verification is real, live code with
zero reachable callers yet. Building a throwaway route to make it
HTTP-testable would test a route that does not exist in production; widening
the sender-kind allowlist to make ``create_identity_session`` reach it would
be shipping the Phase 3 behaviour change a phase early. Neither is this
change's to make (see the plan's §2.3 and §2.5 "not here" notes).

Precedent for this exact move — a Rule-1 exemption for a property with no
HTTP surface, documented at length rather than silently taken —
is ``tests/api/routing/routing_persist_session_ownership_test.py``'s
"DIRECT-SERVICE EXEMPTION" section. Same shape here: every row the test reads
(agent, binding, assignment) is created and mutated through the identity/agent
APIs; only the call to ``ChannelIngestionService.assert_access`` itself goes
around HTTP, because there is no HTTP to go through.

WHAT "ACCEPTS A GRANT" ACTUALLY PROVES HERE, AND WHAT IT DOES NOT
--------------------------------------------------------------------
The plan's test list (§5) asks for two things under one bullet: "assert_access
accepts a valid identity grant" and "the session lands in the owner's space
with the caller stamped". These are proved by two DIFFERENT tests here,
because they exercise two different, non-overlapping code paths:

- ``test_assert_access_accepts_a_valid_grant_on_the_channel_caller_arm``
  calls ``assert_access`` directly with a ``channel_caller`` sender — this is
  the ONLY arm that consults ``policy.identity_grant`` at all, and it proves
  the grant's six conditions re-verify cleanly when every one of them holds.

- ``test_create_identity_session_stamps_owner_and_caller`` drives the real,
  live production path instead — ``mcp_caller`` sender via
  ``create_identity_session`` — which is what actually creates and stamps an
  identity session today. Its ``assert_access`` call passes trivially (the
  ``mcp_caller`` arm does not consult the grant at all — see that arm's
  comment in ``channel_ingestion_service.py``), so this test proves session
  ownership/stamping but proves NOTHING about the six-condition
  re-verification. Do not read it as covering that; the six-condition tests
  below are the only tests that do.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models import Agent, ChannelAccessPolicy, IdentityGrant, SessionSender
from app.services.sessions.channel_ingestion_service import ChannelIngestionService
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import (
    create_identity_binding,
    toggle_identity_contact,
    update_identity_binding,
)
from tests.utils.user import create_random_user, create_random_user_with_headers

API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# Setup helpers — every row created via the identity/agent APIs.
# ---------------------------------------------------------------------------


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{API}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


def _binding_and_assignment(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    agent_id: str,
    caller_id: str,
    caller_headers: dict[str, str],
    enabled: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a binding naming ``caller_id``, optionally opted in. Returns (binding_id, assignment_id)."""
    binding = create_identity_binding(
        client,
        owner_headers,
        agent_id=agent_id,
        trigger_prompt="Route to this agent.",
        assigned_user_ids=[caller_id],
    )
    assignments = binding["assignments"]
    assert len(assignments) == 1, assignments
    if enabled:
        owner_id = _owner_id_of(client, owner_headers)
        toggle_identity_contact(client, caller_headers, owner_id, is_enabled=True)
    return uuid.UUID(binding["id"]), uuid.UUID(assignments[0]["id"])


def _owner_id_of(client: TestClient, owner_headers: dict[str, str]) -> str:
    return _me(client, owner_headers)["id"]


def _agent_row(db, agent_id: str) -> Agent:
    agent = db.get(Agent, uuid.UUID(agent_id))
    assert agent is not None
    return agent


def _channel_caller_sender(caller_id: uuid.UUID) -> SessionSender:
    return SessionSender(
        kind="channel_caller",
        external_id=f"test_channel:{caller_id}",
        display_name=None,
        platform_user_id=caller_id,
    )


# ---------------------------------------------------------------------------
# Item 3(a): the grant arm accepts a genuinely live grant.
# ---------------------------------------------------------------------------


def test_assert_access_accepts_a_valid_grant_on_the_channel_caller_arm(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Every one of the six conditions holds → ``assert_access`` returns
    normally (no ``PermissionError``). The three-way owner invariant is
    deliberately made to fail first (sender.platform_user_id is the CALLER,
    not the owner) so the grant branch is what actually runs, not a
    fast-path that never reached it.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Grant Accept Agent")
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )

    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    sender = _channel_caller_sender(caller_id)
    agent_row = _agent_row(db, agent["id"])

    # No exception — this is the assertion.
    ChannelIngestionService.assert_access(db=db, agent=agent_row, sender=sender, policy=policy)


# ---------------------------------------------------------------------------
# Item 3(b): the live mcp_caller path stamps ownership correctly.
# ---------------------------------------------------------------------------


def test_create_identity_session_stamps_owner_and_caller(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    ``create_identity_session`` (the real, live identity_mcp path — see the
    module docstring for why this is a SEPARATE test from the grant-arm
    acceptance test above) creates the session in the OWNER's space, not the
    caller's, with the caller stamped separately:

      1. Owner's agent has an active environment (required for session
         creation).
      2. A valid IdentityGrant is built from a real binding/assignment.
      3. ``create_identity_session`` is called with an ``mcp_caller`` sender
         (``SessionSender.from_app_mcp``) — the only sender kind this method
         accepts today.
      4. The returned Session: ``user_id == owner`` (not caller),
         ``identity_caller_id == caller``, and both identity linkage columns
         match the grant. ``user_id`` is also confirmed via
         ``GET /sessions/{id}`` (API-observable); the identity-specific
         columns have no API projection today (only ``caller_id``/
         ``caller_email`` do — see ``app/models/sessions/session.py``'s
         ``SessionPublic``), so those are read off the returned ORM row,
         consistent with this file's documented exemption.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Grant Session Agent")
    drain_tasks()  # provision the environment (background task)
    agent = client.get(f"{API}/agents/{agent['id']}", headers=superuser_token_headers).json()
    assert agent["active_environment_id"] is not None

    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )
    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)

    agent_row = _agent_row(db, agent["id"])
    sender = SessionSender.from_app_mcp(caller_id, identity_caller_user_id=caller_id)

    session = ChannelIngestionService.create_identity_session(
        db=db,
        agent=agent_row,
        sender=sender,
        grant=grant,
        integration_type="identity_mcp",
    )

    assert session.user_id == owner_id, "session must be owned by the identity OWNER"
    assert session.identity_caller_id == caller_id
    assert session.identity_binding_id == binding_id
    assert session.identity_binding_assignment_id == assignment_id
    assert session.integration_type == "identity_mcp"

    # API-observable half: the owner sees the session under their own id.
    r = client.get(f"{API}/sessions/{session.id}", headers=superuser_token_headers)
    assert r.status_code == 200
    assert r.json()["user_id"] == str(owner_id)


# ---------------------------------------------------------------------------
# Item 4: each of the six conditions, violated individually.
# ---------------------------------------------------------------------------


def test_assert_access_rejects_condition_1_binding_inactive(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """Condition 1: the binding exists but ``is_active=False``."""
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Cond1 Agent")
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )
    update_identity_binding(client, superuser_token_headers, str(binding_id), is_active=False)

    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    agent_row = _agent_row(db, agent["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_id), policy=policy
        )


def test_assert_access_rejects_condition_2_assignment_not_enabled(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """Condition 2: the assignment exists but was never opted in (``is_enabled=False``)."""
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Cond2 Agent")
    # enabled=False: the caller never toggled the contact on.
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
        enabled=False,
    )

    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    agent_row = _agent_row(db, agent["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_id), policy=policy
        )


def test_assert_access_rejects_condition_3_assignment_belongs_to_different_binding(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Condition 3: ``assignment.binding_id != binding.id``. Both the named
    binding and the named assignment are individually live — assembled from
    two DIFFERENT, otherwise-valid authorizations for the same caller.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent_1 = create_agent_via_api(client, superuser_token_headers, name="Cond3 Agent One")
    agent_2 = create_agent_via_api(client, superuser_token_headers, name="Cond3 Agent Two")
    binding_1_id, _assignment_1_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent_1["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )
    _binding_2_id, assignment_2_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent_2["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )

    # binding_1 (live) paired with assignment_2 (live, but belongs to binding_2).
    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_1_id, assignment_id=assignment_2_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    agent_row = _agent_row(db, agent_1["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_id), policy=policy
        )


def test_assert_access_rejects_condition_4_assignment_issued_to_different_caller(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Condition 4: ``assignment.target_user_id != caller_user_id``. The same
    binding names TWO callers; the grant carries the OTHER caller's
    assignment while the sender claims to be caller 1.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller_1, caller_1_headers = create_random_user_with_headers(client)
    caller_2, caller_2_headers = create_random_user_with_headers(client)
    caller_1_id = uuid.UUID(caller_1["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Cond4 Agent")
    binding = create_identity_binding(
        client, superuser_token_headers, agent_id=agent["id"],
        trigger_prompt="Route to this agent.",
        assigned_user_ids=[caller_1["id"], caller_2["id"]],
    )
    binding_id = uuid.UUID(binding["id"])
    assignments = {a["target_user_id"]: uuid.UUID(a["id"]) for a in binding["assignments"]}
    toggle_identity_contact(client, caller_1_headers, owner["id"], is_enabled=True)
    toggle_identity_contact(client, caller_2_headers, owner["id"], is_enabled=True)
    caller_2_assignment_id = assignments[caller_2["id"]]

    # Grant names caller_2's assignment; sender claims to be caller_1.
    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=caller_2_assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    agent_row = _agent_row(db, agent["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_1_id), policy=policy
        )


def test_assert_access_rejects_condition_5_binding_exposes_a_different_agent(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Condition 5: ``binding.agent_id != agent_id``. The grant's binding/
    assignment are fully live and correctly linked to each other and to the
    caller — but the ``agent`` handed to ``assert_access`` is a DIFFERENT
    agent than the one the binding actually exposes.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent_1 = create_agent_via_api(client, superuser_token_headers, name="Cond5 Agent One")
    agent_2 = create_agent_via_api(client, superuser_token_headers, name="Cond5 Agent Two")
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent_1["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )

    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    # The binding exposes agent_1; hand assert_access agent_2 instead.
    agent_row = _agent_row(db, agent_2["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_id), policy=policy
        )


def test_assert_access_rejects_condition_6_owner_mismatch(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    Condition 6: ``binding.owner_id == agent.owner_id == owner_id`` — the
    binding/assignment/agent triple is fully, correctly linked (conditions
    1-5 all individually hold), but the grant CLAIMS a different owner than
    the one who actually owns the binding.
    """
    owner = _me(client, superuser_token_headers)
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])
    # An unrelated third user — the wrongly-claimed owner. Needs no bindings
    # of their own; the claim itself is what must fail.
    impostor_owner = create_random_user(client)
    impostor_owner_id = uuid.UUID(impostor_owner["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Cond6 Agent")
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )

    grant = IdentityGrant(
        owner_id=impostor_owner_id, binding_id=binding_id, assignment_id=assignment_id
    )
    # expected_owner_id is the AGENT's real owner (what routing established),
    # independent of the grant's (wrong) claim.
    policy = ChannelAccessPolicy(
        expected_owner_id=uuid.UUID(owner["id"]), identity_grant=grant
    )
    agent_row = _agent_row(db, agent["id"])

    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(
            db=db, agent=agent_row, sender=_channel_caller_sender(caller_id), policy=policy
        )


# ---------------------------------------------------------------------------
# Item 5: revoked between the routing decision and ingest (TOCTOU).
# ---------------------------------------------------------------------------


def test_assert_access_rejects_a_grant_deactivated_after_it_was_issued(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    The whole reason ``assert_access`` re-reads rather than trusts: a grant
    that was genuinely valid at routing time must be rejected if the owner
    revokes before ingest catches up (worker-thread hop / auto-install wait
    in between, per the plan's §2.4).

      1. Grant is valid — proved by calling ``assert_access`` once and
         getting no exception (same ids the second call will use).
      2. Owner deactivates the binding via the API.
      3. The SAME grant object, re-submitted, is now rejected.
    """
    owner = _me(client, superuser_token_headers)
    owner_id = uuid.UUID(owner["id"])
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])

    agent = create_agent_via_api(client, superuser_token_headers, name="Toctou Agent")
    binding_id, assignment_id = _binding_and_assignment(
        client, superuser_token_headers,
        agent_id=agent["id"], caller_id=caller["id"], caller_headers=caller_headers,
    )
    grant = IdentityGrant(owner_id=owner_id, binding_id=binding_id, assignment_id=assignment_id)
    policy = ChannelAccessPolicy(expected_owner_id=owner_id, identity_grant=grant)
    agent_row = _agent_row(db, agent["id"])
    sender = _channel_caller_sender(caller_id)

    # 1. Valid at issue time.
    ChannelIngestionService.assert_access(db=db, agent=agent_row, sender=sender, policy=policy)

    # 2. Owner revokes — simulates the window between routing and ingest.
    update_identity_binding(client, superuser_token_headers, str(binding_id), is_active=False)

    # 3. Same grant, re-read, now rejected.
    with pytest.raises(PermissionError):
        ChannelIngestionService.assert_access(db=db, agent=agent_row, sender=sender, policy=policy)
