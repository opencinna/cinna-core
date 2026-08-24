"""The reachability verdict — plan §9's headline output, one test per branch.

`GET /admin/routing/traces/{id}` carries a `diagnosis`: one server-authored
sentence saying why the decision went this way and what to change, plus a
Jaccard near-miss ranking. `?expected_agent_id=` narrows it to "why was THIS
agent not a candidate", which is the question the tuning card is actually
opened to answer.

**Why every test here asserts the sentence, not just the code.** The wording
*is* the feature for the motivating case (plan §2, Bug 2: a standalone agent
has no `AppAgentRoute`, so it is absent from `get_effective_routes_for_user`
and the classifier never sees it — deliberately not fixed by changing the
auto-route rule, fixed by saying so). A test that pinned only `code` would keep
passing while the sentence drifted into saying something false, and a wrong
diagnosis about somebody else's agent is worse than no diagnosis. So each test
below pins the exact text of the branch it exercises. That makes them
deliberately brittle to rewording, which is the intended trade: reword the
verdict, update the test, and the update is where somebody re-reads whether the
new sentence is still true.

**Every branch is reached through a real routing decision** (`POST
/admin/routing/simulate` — no side effects, and it runs the real router over
the target's real state), never by hand-building a trace. The verdict is a
statement about what routing did; a fabricated trace would let it be right
about a decision the router cannot actually produce.

The producer is simulate rather than a webhook delivery because the branches
here are about *routing state*, not delivery: simulate reaches the same
`ChannelRoutingService.decide` with three fewer moving parts and no channel to
set up. Where a branch needs configuration that changed *after* the decision
(the `looks_reachable` / `route_inactive` family), the route is created between
the simulate and the read — which is the real-world shape of those branches too.

Unit tests for the same service's pure parts — totality under a poison object,
the reuse of `AppAgentRouteService`'s Jaccard helpers, candidate
de-duplication across stages — live in `tests/unit/test_routing_reachability.py`.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_admin_route, create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.routing import (
    get_routing_trace,
    patched_routing_externals,
    simulate_routing,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _user(client: TestClient, superuser_headers: dict[str, str]) -> tuple[dict, dict]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _agent(client: TestClient, headers: dict[str, str], label: str) -> dict:
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    return agent


def _routes(
    client: TestClient, headers: dict[str, str], agents: list[dict]
) -> None:
    for agent in agents:
        create_user_route(
            client, headers, agent["id"], trigger_prompt=f"Handle {agent['name']} work"
        )


def _simulate_no_match(
    client: TestClient, superuser_headers: dict[str, str], user_id: str, message: str
) -> dict:
    """A decision where the classifier ran and picked nothing.

    `classify_no_match` rather than leaving the classifier unpatched: there is
    no LLM in this environment, and an unpatched call would either reach a real
    provider or fail for a reason that has nothing to do with the branch under
    test.
    """
    with patched_routing_externals(classify_no_match=True):
        return simulate_routing(
            client, superuser_headers, message=message, as_user_id=user_id
        )


def _diagnosis(
    client: TestClient,
    superuser_headers: dict[str, str],
    trace: dict,
    *,
    expected_agent_id: str | None = None,
) -> dict:
    detail = get_routing_trace(
        client, superuser_headers, trace["id"], expected_agent_id=expected_agent_id
    )
    assert detail["diagnosis"] is not None, detail
    return detail["diagnosis"]


# ---------------------------------------------------------------------------
# General verdicts — no expected agent named
# ---------------------------------------------------------------------------


def test_verdict_when_the_user_has_no_candidates_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A user with no route and an empty auto-install list.

    The most common shape of "the bot didn't find my agent" on a fresh
    deployment, and the one an empty candidate table cannot explain on its own.
    """
    user, _ = _user(client, superuser_token_headers)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    assert trace["outcome"] == "no_match", trace

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: no App MCP route reaches "
        "them and no auto-install bundle was eligible, so no message from them "
        "can route anywhere. Give the agent you expected an App MCP route from "
        "its Integrations tab, or add its bundle to the auto-install list."
    )
    assert diagnosis["eligible_candidate_count"] == 0
    # The remedy is a substring of the sentence by construction, so a client
    # rendering them separately cannot show two different answers.
    assert diagnosis["action"] in diagnosis["verdict"]


def test_verdict_when_routes_exist_but_the_classifier_matched_none(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Three eligible routes, classifier picks nothing — plan §9's count."""
    user, headers = _user(client, superuser_token_headers)
    agents = [_agent(client, headers, f"NoMatch{i}") for i in range(3)]
    _routes(client, headers, agents)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "solve this equation"
    )
    diagnosis = _diagnosis(client, superuser_token_headers, trace)

    assert diagnosis["code"] == "no_match"
    assert diagnosis["eligible_candidate_count"] == 3
    assert diagnosis["verdict"] == (
        "This user has 3 effective routes and the classifier matched none of "
        "them. Widen the trigger prompt of the agent that should have won — "
        "the near-miss scores below say which came closest — or use Draft a "
        "recommendation to generate wording for its owner."
    )


def test_verdict_when_every_candidate_was_excluded_before_the_classifier(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """One route, switched off.

    The route is still recorded as a candidate (with `route_inactive`) rather
    than dropped, which is the only reason this branch can say "excluded"
    instead of "you have nothing".
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Inactive")
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle it", is_active=False
    )

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please handle it"
    )
    diagnosis = _diagnosis(client, superuser_token_headers, trace)

    assert diagnosis["code"] == "all_candidates_skipped"
    assert diagnosis["skipped_by_reason"] == {"route_inactive": 1}
    assert diagnosis["verdict"] == (
        "This user has no eligible routes: 1 candidate was excluded before the "
        "classifier saw it (route_inactive). Fix the exclusion on the agent "
        "you expected — the candidate table below names the reason for each "
        "one."
    )


def test_verdict_when_the_decision_routed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A success is a verdict too — and it must not read as a problem."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Winner")
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    with patched_routing_externals():
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=user["id"],
        )
    assert trace["outcome"] == "routed", trace

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "routed"
    assert diagnosis["verdict"] == (
        f"This message routed to {agent['name']}, chosen from 1 eligible "
        f"candidate. Nothing to fix here. If it reached the wrong agent, "
        f"compare the near-miss scores below and tighten the winner's trigger "
        f"prompt so it stops claiming this kind of message."
    )


def test_verdict_when_the_routing_pass_itself_failed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`outcome=error` points at the provider cascade, not at any agent.

    The failing pass persists its own trace before re-raising, and simulate
    reports the failure with a 500 that names where to find it — so the row is
    fetched from the trace list rather than from the simulate response.
    """
    from tests.utils.routing import list_routing_traces

    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Boom")
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    with patched_routing_externals():
        from unittest.mock import patch

        with patch(
            "app.services.server_channels.channel_routing_service."
            "ChannelRoutingService._route_installed",
            side_effect=RuntimeError("provider exploded"),
        ):
            simulate_routing(
                client,
                superuser_token_headers,
                message="please handle this",
                as_user_id=user["id"],
                expected_status=500,
            )

    page = list_routing_traces(
        client, superuser_token_headers, origin="simulate", outcome="error"
    )
    assert page["count"] == 1, page
    diagnosis = _diagnosis(client, superuser_token_headers, page["data"][0])

    assert diagnosis["code"] == "error"
    assert diagnosis["verdict"] == (
        "This decision failed before it reached a verdict: RuntimeError. Check "
        "the provider attempts below — a routing failure with no attempt at "
        "all means no AI credential was usable, which is a server "
        "configuration problem rather than an agent one."
    )


# ---------------------------------------------------------------------------
# Expected-agent verdicts answered from the trace
# ---------------------------------------------------------------------------


def test_verdict_when_the_expected_agent_was_the_one_chosen(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Chosen")
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    with patched_routing_externals():
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=user["id"],
        )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_selected"
    assert diagnosis["verdict"] == (
        f"{agent['name']} is the agent this decision chose. Nothing to fix — "
        f"if the message still went nowhere, the failure is after routing "
        f"(session setup or the outbound reply), not in it."
    )
    assert diagnosis["expected_agent_id"] == agent["id"]
    assert diagnosis["expected_agent_name"] == agent["name"]


def test_verdict_when_the_expected_agent_was_eligible_but_not_picked(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Reachability is fine; the classifier is the problem. Say exactly that.

    The trigger prompt is written to share tokens with the message so the
    near-miss score is non-zero and lands in the sentence — that overlap number
    is the difference between "it lost" and "it lost narrowly".
    """
    user, headers = _user(client, superuser_token_headers)
    agents = [_agent(client, headers, f"Considered{i}") for i in range(2)]
    create_user_route(
        client,
        headers,
        agents[0]["id"],
        trigger_prompt="eigenvalue matrix questions",
    )
    create_user_route(
        client, headers, agents[1]["id"], trigger_prompt="calendar booking requests"
    )

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "eigenvalue matrix questions"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agents[0]["id"]
    )

    assert diagnosis["code"] == "expected_agent_considered"
    assert diagnosis["verdict"] == (
        f"{agents[0]['name']} was an eligible candidate (token overlap 1.00) "
        f"and the classifier did not pick it — reachability is not the problem "
        f"here. Widen its trigger prompt to cover wording like this message, "
        f"or use Draft a recommendation to generate that wording for its owner."
    )


def test_verdict_when_the_expected_agents_route_is_switched_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The trace answers this one — an inactive route IS recorded, as a skip."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "SwitchedOff")
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle it", is_active=False
    )

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please handle it"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_skipped"
    assert diagnosis["verdict"] == (
        f"{agent['name']} was considered for this decision and then excluded: "
        f"its App MCP route exists but is switched off. Switch the route back "
        f"on from the agent's Integrations tab."
    )


def test_verdict_when_the_expected_agent_belongs_to_another_account(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A foreign owner's agent that reached Pass 1 and was then rejected.

    Reached by assigning an admin route on the *owner's* agent to a different
    sender: the route makes it a candidate, and the ownership filter throws it
    out — the exact `foreign_owner` skip the router records, rather than a
    hand-built one.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _sender_headers = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "Foreign")
    create_admin_route(
        client,
        superuser_token_headers,
        agent["id"],
        trigger_prompt="Handle anything at all",
        assigned_user_ids=[sender["id"]],
        # Superuser-only, and required: an assignment created for somebody
        # other than the route's creator lands ``is_enabled=False`` unless this
        # is set, which would make the route unreachable and send this scenario
        # into the *unassigned* branch instead of the ownership one.
        auto_enable_for_users=True,
    )

    with patched_routing_externals():
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=sender["id"],
        )
    assert trace["outcome"] == "no_match", trace

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_skipped"
    assert diagnosis["verdict"] == (
        f"{agent['name']} was considered for this decision and then excluded: "
        f"it belongs to a different account, and a channel session must run on "
        f"the sender's own install. Share the agent's bundle with this user "
        f"and have them install it — routing to somebody else's install is "
        f"refused by design."
    )


def test_verdict_when_the_expected_agent_was_reached_as_an_identity_contact(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An identity contact route is not selectable from a channel.

    Driven through the real identity plumbing: a binding on the owner's agent,
    assigned to the sender, makes the contact an effective route; Pass 1 matches
    it and `ChannelRoutingService._route_installed` records the
    `identity_route` skip.
    """
    from tests.utils.identity import (
        create_identity_binding,
        toggle_identity_contact,
    )

    owner, owner_headers = _user(client, superuser_token_headers)
    sender, sender_headers = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "IdentityAgent")
    create_identity_binding(
        client,
        owner_headers,
        agent["id"],
        trigger_prompt="Handle anything at all",
        assigned_user_ids=[sender["id"]],
    )
    # The recipient enables the contact. An assignment made for somebody other
    # than the binding's owner lands disabled, and `auto_enable=True` is
    # administrator-only — so the contact becomes an effective route the way it
    # does in production: the person on the receiving end turns it on.
    toggle_identity_contact(client, sender_headers, owner["id"], True)

    with patched_routing_externals():
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=sender["id"],
        )
    assert trace["outcome"] == "no_match", trace

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_skipped"
    assert diagnosis["verdict"] == (
        f"{agent['name']} was considered for this decision and then excluded: "
        f"it was reached through an identity contact route, which hands off to "
        f"that person's agents in a second stage and is not selectable from a "
        f"channel. Route to the contact rather than to their agent, or give "
        f"this user their own install of it."
    )


# ---------------------------------------------------------------------------
# Expected-agent verdicts answered from current configuration
# ---------------------------------------------------------------------------


def test_verdict_for_an_agent_id_that_does_not_exist(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user, _ = _user(client, superuser_token_headers)
    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    ghost = str(uuid.uuid4())

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=ghost
    )
    assert diagnosis["code"] == "expected_agent_unknown"
    assert diagnosis["verdict"] == (
        f"No agent {ghost} exists on this server, so it could never have been "
        f"a routing candidate. Check the id against the agent's own page — a "
        f"deleted agent and a mistyped id look the same from here."
    )


def test_verdict_for_a_standalone_agent_with_no_app_mcp_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """**The motivating bug** (plan §2, Bug 2 / §9's sentence).

    `_create_auto_route_for_agent` returns `None` when `bundle_uuid IS NULL`,
    so a standalone agent never gets an automatic route, is absent from
    `get_effective_routes_for_user`, and the classifier never sees it. Nothing
    in the trace mentions it — that is the whole difficulty — so the verdict is
    answered from configuration.

    Three other routes exist so the count in the sentence is the plan's own
    number and so "not among them" has a "them" to be absent from.
    """
    user, headers = _user(client, superuser_token_headers)
    _routes(client, headers, [_agent(client, headers, f"Other{i}") for i in range(3)])
    orphan = _agent(client, headers, "Standalone")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "solve this equation"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=orphan["id"]
    )

    assert diagnosis["code"] == "expected_agent_standalone_no_route"
    assert diagnosis["verdict"] == (
        f"This user has 3 effective routes; {orphan['name']} is not among them "
        f"because it is not a bundle install and has no App MCP route. Add one "
        f"from its Integrations tab. (Setting its router trigger prompt alone "
        f"will not do it: a standalone agent never gets an automatic route — "
        f"that is deliberate, its owner manages App MCP exposure explicitly.)"
    )
    # The agent is genuinely absent from the trace: this verdict came from
    # configuration, which is the only place the answer exists.
    refs = {
        c["ref_id"]
        for stage in get_routing_trace(
            client, superuser_token_headers, trace["id"]
        )["stages"]
        for c in stage["candidates"]
    }
    assert orphan["id"] not in refs, refs


def test_verdict_for_a_bundle_install_whose_revision_has_no_trigger_prompt(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """No prompt means no auto-route was ever created for the install."""
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    source = _agent(client, publisher_headers, "NoPrompt")
    publish_bundle_and_make_public(client, publisher_headers, source["id"])
    bundle_id = client.get(
        f"{API}/agents/{source['id']}", headers=publisher_headers
    ).json()["bundle_id"]

    consumer, consumer_headers = _user(client, superuser_token_headers)
    installed = install_bundle(client, consumer_headers, bundle_id)

    trace = _simulate_no_match(
        client, superuser_token_headers, consumer["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=installed["id"]
    )

    assert diagnosis["code"] == "expected_agent_no_trigger_prompt"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {installed['name']} is not among "
        f"them because it has no router trigger prompt, so no route was ever "
        f"created for its install and the classifier would have had nothing to "
        f"match on anyway. Set a router trigger prompt on the agent's "
        f"Configuration tab — for a bundle install that creates the App MCP "
        f"route automatically."
    )


def test_verdict_for_a_bundle_install_whose_route_was_deleted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Trigger prompt set, route gone — a different repair from the no-prompt one."""
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    source = _agent(client, publisher_headers, "HasPrompt")
    r = client.patch(
        f"{API}/agents/{source['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle bundle routing questions"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, source["id"])
    bundle_id = client.get(
        f"{API}/agents/{source['id']}", headers=publisher_headers
    ).json()["bundle_id"]

    consumer, consumer_headers = _user(client, superuser_token_headers)
    installed = install_bundle(client, consumer_headers, bundle_id)

    routes = client.get(
        f"{API}/agents/{installed['id']}/app-mcp-routes/", headers=consumer_headers
    )
    assert routes.status_code == 200, routes.text
    assert routes.json(), "precondition: the install auto-created a route"
    for route in routes.json():
        d = client.delete(
            f"{API}/agents/{installed['id']}/app-mcp-routes/{route['id']}",
            headers=consumer_headers,
        )
        assert d.status_code == 200, d.text

    trace = _simulate_no_match(
        client, superuser_token_headers, consumer["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=installed["id"]
    )

    assert diagnosis["code"] == "expected_agent_bundle_no_route"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {installed['name']} is not among "
        f"them because it has no App MCP route, even though its router trigger "
        f"prompt is set. Add a route from its Integrations tab. An install "
        f"whose revision carried no trigger prompt at the time gets no "
        f"automatic route, and re-saving the prompt is what normally backfills "
        f"it."
    )


def test_verdict_for_a_foreign_agent_with_no_route_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Never a candidate, and the reason is ownership rather than a switch."""
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")

    trace = _simulate_no_match(
        client, superuser_token_headers, sender["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_foreign_owner"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because it belongs to a different account and has no route assigning "
        f"it to this user — a channel session runs on the sender's own "
        f"install. Share its bundle with this user and have them install it, "
        f"or assign them to an App MCP route on it."
    )
    assert diagnosis["expected_agent_owner_email"] == owner["email"]


def test_verdict_when_the_route_exists_but_is_not_assigned_to_this_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An admin route with nobody assigned is invisible to every sender."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Unassigned")
    create_admin_route(
        client,
        superuser_token_headers,
        agent["id"],
        trigger_prompt="Handle anything at all",
        assigned_user_ids=[],
    )

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_route_unassigned"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because its App MCP route is not assigned to this user, or the "
        f"assignment is switched off. Assign this user to the route from the "
        f"route's Users list, and check the per-user toggle is on."
    )


def test_verdict_when_the_route_is_not_enabled_for_the_app_mcp_channel(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Created after the decision — an active route off the App MCP channel."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "WrongChannel")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    create_user_route(
        client,
        headers,
        agent["id"],
        trigger_prompt="Handle it",
        channel_app_mcp=False,
    )

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_route_not_app_mcp"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because its route is not enabled for the App MCP channel, which is "
        f"the channel this decision routed over. Turn on App MCP for that "
        f"route from the agent's Integrations tab."
    )


def test_verdict_when_the_route_was_switched_off_after_the_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The configuration branch for an inactive route.

    Distinct from the trace-answered `expected_agent_skipped` form above: there
    the route existed when the decision ran and was recorded as a skip; here it
    did not exist at all, so the answer has to come from what is configured now.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "LaterOff")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle it", is_active=False
    )

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_route_inactive"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because its App MCP route exists but is switched off. Switch the "
        f"route back on from the agent's Integrations tab."
    )


def test_verdict_when_the_route_was_added_after_the_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The one branch whose remedy is "re-run", not "change something".

    Without it, an admin who has just fixed the route would read a stale trace
    as evidence the fix did not work.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "FixedSince")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle it")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_looks_reachable"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because it was not a candidate when this decision ran, even though "
        f"its App MCP route looks correctly configured now. Re-run this "
        f"decision — a route added or switched on after the trace was captured "
        f"explains exactly this, and the re-run will show it as a candidate."
    )


# ---------------------------------------------------------------------------
# Near-miss ranking (plan §3 — the Jaccard helpers, reused)
# ---------------------------------------------------------------------------


def test_near_misses_are_ranked_best_first_against_the_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """"closest: Equation Assistant 0.31" — the §9 output, ranked server-side.

    Two routes whose trigger prompts share very different amounts of vocabulary
    with the message. Asserting the *order* rather than a hard-coded score:
    the ranking is what the card reads, and pinning exact Jaccard values here
    would turn a tuning change to the shared tokenizer into a failure in the
    wrong file.
    """
    user, headers = _user(client, superuser_token_headers)
    close = _agent(client, headers, "Equation")
    far = _agent(client, headers, "Calendar")
    create_user_route(
        client, headers, close["id"], trigger_prompt="eigenvalue matrix questions"
    )
    create_user_route(
        client, headers, far["id"], trigger_prompt="booking holiday travel plans"
    )

    trace = _simulate_no_match(
        client,
        superuser_token_headers,
        user["id"],
        "can you check my eigenvalue matrix",
    )
    diagnosis = _diagnosis(client, superuser_token_headers, trace)

    ranked = diagnosis["near_misses"]
    assert [m["name"] for m in ranked] == [close["name"], far["name"]], ranked
    assert ranked[0]["similarity"] > ranked[1]["similarity"], ranked
    assert ranked[1]["similarity"] == 0.0, ranked
    assert diagnosis["near_miss_notice"] is None


def test_near_misses_go_quiet_rather_than_empty_when_the_text_is_gated(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """With `ROUTING_TRACE_STORE_MESSAGE_TEXT` off there is nothing to rank
    against, and an empty ranking would read as "nothing came close" — a
    finding, not a gap. It has to be distinguishable, so it carries a notice
    (§11a Rule 1 on a read surface).

    The row is written with the gate *on* and read with it *off*, so what is
    being tested is the diagnosis reacting to the served projection rather than
    to an empty row: the candidates and the verdict survive, the ranking does
    not.
    """
    from unittest.mock import patch

    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Gated")
    create_user_route(
        client, headers, agent["id"], trigger_prompt="eigenvalue matrix questions"
    )

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "eigenvalue matrix please"
    )
    with_text = _diagnosis(client, superuser_token_headers, trace)
    assert with_text["near_misses"], with_text

    with patch("app.core.config.settings.ROUTING_TRACE_STORE_MESSAGE_TEXT", False):
        gated = _diagnosis(client, superuser_token_headers, trace)

    assert gated["near_misses"] == []
    assert gated["near_miss_notice"] == (
        "Near-miss ranking needs the message that was routed, and this trace's "
        "text is not available — ROUTING_TRACE_STORE_MESSAGE_TEXT is off now, "
        "or was off when it was captured. The candidate list and the verdict "
        "are unaffected."
    )
    # The verdict itself is unaffected: it is built from the candidate set,
    # which the allowlist keeps serving.
    assert gated["code"] == with_text["code"]
    assert gated["eligible_candidate_count"] == 1


def test_simulate_and_trace_detail_return_the_same_diagnosis(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A simulate response is `RoutingTraceService.get`'s output, diagnosis
    included — not a matching projection built next to it. This is the property
    that keeps the two from drifting apart the next time either grows a field.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Same")
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")

    with patched_routing_externals():
        simulated = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=user["id"],
        )
    fetched = get_routing_trace(client, superuser_token_headers, simulated["id"])

    assert simulated["diagnosis"] is not None
    assert simulated["diagnosis"] == fetched["diagnosis"]
