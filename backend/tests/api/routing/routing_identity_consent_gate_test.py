"""The identity consent gate is the PROVIDER's, not each surface's.

WHAT THIS PINS
--------------
``allow_identity_routing`` is the sender's own consent that a message of theirs
may open a session in **somebody else's workspace**, where that person can read
it (master plan §3.4 — "anything that can route into another person's workspace
is opt-in, per person"). Until phase 7 of
``docs/plans/channels_identity_unification/`` that gate was an ``if`` written
out at each of the three surfaces which compose
``IdentityCandidateProvider``: channel routing, App MCP routing, and App MCP's
``prompts/list`` discovery. Three copies of one consent check, over one shared
provider, where the cost of a missing copy is a stranger's message landing in a
stranger's workspace.

It now lives inside ``IdentityCandidateProvider.build``, which refuses when the
policy it is handed says the caller has not opted in. This file asserts the
*relocation* — that the refusal is really the provider's, and that it is
observable as such from every one of the three consumer paths, so a fourth
consumer inherits it without knowing it exists.

WHAT IT DOES **NOT** PIN, AND WHY IT WOULD BE THE WRONG FILE FOR IT
--------------------------------------------------------------------
Not "identity routing works when it is switched on", and not the revocation
paths. Those are the feature, and they are covered where the feature lives:
``tests/api/server_channels/server_channels_identity_routing_test.py`` and
``..._identity_trace_test.py`` for the channel half,
``tests/api/app_mcp/app_mcp_session_test.py`` for App MCP's resume-time
re-check, and
``tests/api/app_mcp/app_mcp_identity_candidate_provider_test.py`` for the
provider's candidate-collapse and skip-boundary parity. Every test below turns
the switch **on** only as the control that makes its "off" assertion
non-vacuous — an empty ballot proves nothing unless the same setup produces a
non-empty one.

The structural half of the same guarantee — that every call site under ``app/``
actually hands its resolved policy in, rather than omitting the keyword and
silently taking the provider's channel-less default — is
``tests/architecture/channel_routing_scope_test.py::
test_every_identity_candidate_build_call_passes_a_policy``. A signature cannot
carry that half and neither can a behavioural test, which can only see the
consumers that exist today.

WHERE THESE ENTER, AND WHY
--------------------------
At the service layer, one function below each surface's own entry point, which
is this area's established convention (``tests/api/app_mcp/README.md``, "there
is no HTTP route to drive"). Every *input* is still built through real routes —
users, agents, router trigger prompts, identity bindings, the per-user channel
setting — and the two surfaces that resolve their own policy
(``AppMCPRoutingService.route_message`` and ``prompts/list``) are driven with
the switch flipped through ``PUT /users/me/channels/{id}``, never with a
hand-made policy object, precisely so that "the caller opted in" means what the
product means by it.

The channel surface is the exception and takes a constructed
``ResolvedChannelPolicy``: ``ChannelRoutingService._route_installed`` is handed
its policy by its caller rather than resolving one, so a constructed policy is
what that boundary actually receives in production. ``tests.utils.
server_channel.route_installed`` is the documented seam for it.

NO CLASSIFIER RUNS IN THIS FILE, AND THAT IS AN ASSERTION
----------------------------------------------------------
Every scenario gives its caller exactly one eligible candidate on the "on"
side, so Stage 1 takes the ``only_one`` short-circuit and never reaches a
model. The autouse ``block_llm_provider`` guard raises a ``BaseException`` on
an unstubbed classify, so "no stub is named here" is what pins that — see
``tests/api/routing/README.md``. Do not add a stub to make a failure go away;
a classify reaching a model in this file means the ballot grew a candidate the
scenario did not intend.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.mcp.app_prompts import register_app_mcp_prompts
from app.mcp.context_vars import mcp_authenticated_user_id_var
from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
from app.services.routing import routing_trace
from app.services.routing.identity_candidate_provider import (
    IdentityCandidateProvider,
    identity_ref_id,
)
from app.services.server_channels.channel_policy_service import ResolvedChannelPolicy
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import share_identity_agent
from tests.utils.server_channel import find_server_channel_by_type, route_installed
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import update_my_channel

APP_MCP = "app_mcp"

CONSENT_OFF = ResolvedChannelPolicy.for_no_channel()
"""`allow_identity_routing=False` while everything else is permissive.

Not a hand-built object: this is the policy the platform itself resolves for a
decision that belongs to no channel, and its own docstring says the asymmetry
is the point — the absence of a channel is nobody's consent. Using it here
means the "off" side of every channel case is the exact value production
produces, rather than a shape invented by a test.
"""

CONSENT_ON = dataclasses.replace(CONSENT_OFF, allow_identity_routing=True)


class _PromptCapturingServer:
    """The one thing ``register_app_mcp_prompts`` needs: a ``prompt`` decorator.

    ``prompts/list`` has no HTTP route and no service class — it is a closure
    registered onto a FastMCP instance — so the only way to reach it is to hand
    the registrar a server that keeps what it is given. Standing up a real
    FastMCP instance would add a protocol layer between the test and the two
    lines under test without adding a single assertion.
    """

    def __init__(self) -> None:
        self.registered: Any = None

    def prompt(self) -> Callable[[Any], Any]:
        def decorator(fn: Any) -> Any:
            self.registered = fn
            return fn

        return decorator


def _discovery_prompt_texts(caller_id: uuid.UUID) -> list[str]:
    """Run App MCP ``prompts/list`` as ``caller_id`` and return its text."""
    server = _PromptCapturingServer()
    register_app_mcp_prompts(server)
    assert server.registered is not None, "no prompt was registered"

    token = mcp_authenticated_user_id_var.set(str(caller_id))
    try:
        messages = asyncio.run(server.registered())
    finally:
        mcp_authenticated_user_id_var.reset(token)
    return [message.content.text for message in messages]


def _identity_owner_with_one_shared_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    caller: dict[str, Any],
    caller_headers: dict[str, str],
) -> dict[str, Any]:
    """An owner who has shared exactly one agent with ``caller``, switched on.

    Both halves matter and are what ``share_identity_agent`` composes: the
    owner assigns, and the caller opts in per-person. This is the state in
    which the *only* thing standing between the caller and that person's
    workspace is the channel-level ``allow_identity_routing`` switch — which is
    the state every assertion in this file is about.
    """
    owner, owner_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner["id"])
    create_random_ai_credential(client, owner_headers, set_default=True)
    agent = create_agent_via_api(client, owner_headers, name="HR Identity Agent")
    drain_tasks()
    set_router_trigger_prompt(
        client, owner_headers, agent["id"], "Answer HR and time-off questions."
    )
    share_identity_agent(
        client,
        owner_headers,
        caller_headers,
        agent_id=agent["id"],
        target_user_id=caller["id"],
        owner_id=owner["id"],
        trigger_prompt="Answer questions about time off and HR policy.",
        enable=True,
    )
    return {"user": owner, "headers": owner_headers, "agent": agent}


# ---------------------------------------------------------------------------
# 1. The provider itself
# ---------------------------------------------------------------------------


def test_provider_refuses_and_records_nothing_when_consent_is_off(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`build()` with consent off returns nothing AND writes no trace row.

    Two assertions, and the second is the one that is easy to lose. Master plan
    §3.5 says every candidate exclusion records a `skip_reason`, because a
    candidate excluded silently cannot diagnose the failure that actually bites
    — *the expected candidate was never on the ballot at all*. Un-opted-in
    identities are the ONE deliberate inversion of that rule in the whole
    feature: recording them would publish the existence of other people's
    identities into a trace an external sender can trigger at will, one row per
    person who has ever named them.

    While the gate was an `if` at three call sites, that silence was structural
    — the provider was simply never called. Moving the gate inside the provider
    moves the silence inside it too, and the failure mode of getting that wrong
    is invisible: the ballot would still be empty and routing would still
    behave identically, while every declined message quietly enumerated a
    person's contacts to whoever can read traces. So it is asserted here as
    *zero rows*, not as zero eligible rows.

    The control at the bottom is what stops this being a test of an empty
    fixture: the same caller, the same database state, one field different.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])
    owner = _identity_owner_with_one_shared_agent(
        client, superuser_token_headers, caller, caller_headers
    )
    owner_ref = identity_ref_id(uuid.UUID(owner["user"]["id"]))

    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        user_id=caller_id,
        message="who handles time off?",
        stage=routing_trace.STAGE_PASS_1,
    ) as trace:
        refused = IdentityCandidateProvider.build(db, caller_id, policy=CONSENT_OFF)

    assert refused == [], (
        "The provider must refuse to produce identity candidates when the "
        f"caller has not opted in; got {[c.ref_id for c in refused]}."
    )
    recorded = [c for stage in trace.stages for c in stage.candidates]
    assert recorded == [], (
        "With consent off the provider must record NOTHING — not even a skip. "
        "A skip row here would name a person the caller never opted into "
        "reaching, in a trace an external sender can trigger at will. Got: "
        f"{[(c.ref_id, c.skip_reason) for c in recorded]}."
    )

    # Control — same state, consent on, so the assertions above are about the
    # switch and not about an empty database.
    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        user_id=caller_id,
        message="who handles time off?",
        stage=routing_trace.STAGE_PASS_1,
    ) as control_trace:
        allowed = IdentityCandidateProvider.build(db, caller_id, policy=CONSENT_ON)

    assert [c.ref_id for c in allowed] == [owner_ref]
    assert owner_ref in {
        c.ref_id for stage in control_trace.stages for c in stage.candidates
    }


# ---------------------------------------------------------------------------
# 2. The three consumer paths
# ---------------------------------------------------------------------------


def test_channel_routing_gate_is_the_providers(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """Channel Pass 1 no longer holds the gate — and behaves as if it did.

    The sender owns no eligible agent of their own, so the identity candidate
    is the entire ballot: with consent off Pass 1 has nothing to route to, and
    with consent on it takes the `only_one` short-circuit straight into
    Stage 2. That the outcome flips on one field, with the `if` deleted from
    this surface, is the whole claim.
    """
    from app.models import User

    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])
    owner = _identity_owner_with_one_shared_agent(
        client, superuser_token_headers, caller, caller_headers
    )
    sender = db.get(User, caller_id)

    with routing_trace.RoutingTrace.capture(
        origin=routing_trace.ORIGIN_SERVER_CHANNEL,
        user_id=caller_id,
        message="who handles time off?",
        stage=routing_trace.STAGE_PASS_1,
    ) as trace:
        declined = route_installed(
            db, sender, "who handles time off?", policy=CONSENT_OFF
        )

    assert declined is None
    assert [c for stage in trace.stages for c in stage.candidates] == [], (
        "A channel decision made without the sender's identity consent must "
        "carry no identity rows at all, eligible or skipped."
    )

    routed = route_installed(db, sender, "who handles time off?", policy=CONSENT_ON)
    assert routed is not None, (
        "Control failed: with consent on the same sender must reach the "
        "identity owner's agent, or the assertion above is about an empty "
        "fixture rather than about the switch."
    )
    assert str(routed.id) == owner["agent"]["id"]


def test_app_mcp_routing_gate_is_the_providers(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """App MCP Stage 1 no longer holds the gate either.

    Unlike the channel case this resolves its own policy, so the switch is
    thrown the way a user throws it — `PUT /users/me/channels/{id}` on the App
    MCP singleton — and the `channel_user_setting` row is created lazily by
    that call, exactly as master plan §3.3 requires.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])
    owner = _identity_owner_with_one_shared_agent(
        client, superuser_token_headers, caller, caller_headers
    )
    channel = find_server_channel_by_type(client, superuser_token_headers, APP_MCP)

    declined = AppMCPRoutingService.route_message(
        db, caller_id, "who handles time off?"
    )
    assert declined is None, (
        "With `allow_identity_routing` never switched on (it defaults false "
        "and never inherits) App MCP must not route into another person's "
        f"workspace; got {declined}."
    )

    update_my_channel(
        client, caller_headers, channel["id"], allow_identity_routing=True
    )
    routed = AppMCPRoutingService.route_message(db, caller_id, "who handles time off?")
    assert routed is not None, "Control failed: consent on must reach the identity."
    assert routed.is_identity is True
    assert str(routed.agent_id) == owner["agent"]["id"]
    assert str(routed.identity_owner_id) == owner["user"]["id"]


def test_app_mcp_prompt_discovery_gate_is_the_providers(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`prompts/list` asks the router's question, gate included.

    This is the surface the relocation protects most. It is a discovery list,
    not a decision, so a dropped gate here leaks rather than routes — the
    caller's MCP client is simply told a person's trigger sentence, and nothing
    fails. It also has no test of its own before this one, which is exactly the
    combination that lets a divergence sit.

    The caller owns one eligible agent throughout, so the "off" assertion is a
    statement about *which* prompts came back rather than about an empty list —
    and an empty list would also be what `register_app_mcp_prompts`' own
    `except Exception` returns, which would otherwise make this test pass on a
    crash.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller["id"])
    promote_to_developer(client, superuser_token_headers, caller["id"])
    create_random_ai_credential(client, caller_headers, set_default=True)
    own_agent = create_agent_via_api(client, caller_headers, name="My Own Agent")
    drain_tasks()
    set_router_trigger_prompt(
        client, caller_headers, own_agent["id"], "Summarise my own notes."
    )
    owner = _identity_owner_with_one_shared_agent(
        client, superuser_token_headers, caller, caller_headers
    )
    channel = find_server_channel_by_type(client, superuser_token_headers, APP_MCP)
    owner_email = owner["user"]["email"]

    without_consent = _discovery_prompt_texts(caller_id)
    assert any("Summarise my own notes." in text for text in without_consent), (
        "The caller's own agent must be discoverable regardless of identity "
        "consent — without it this test's negative assertion would pass on a "
        "swallowed exception."
    )
    assert not any(owner_email in text for text in without_consent), (
        "Discovery must not name a person the caller has not opted into "
        f"reaching. Got: {without_consent}"
    )

    update_my_channel(
        client, caller_headers, channel["id"], allow_identity_routing=True
    )
    with_consent = _discovery_prompt_texts(caller_id)
    assert any("Summarise my own notes." in text for text in with_consent)
    assert any(owner_email in text for text in with_consent), (
        "Control failed: with consent on, discovery must offer the identity "
        f"owner the router would accept. Got: {with_consent}"
    )
