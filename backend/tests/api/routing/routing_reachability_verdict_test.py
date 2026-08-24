"""The reachability verdict — plan §9's headline output, one test per branch.

`GET /admin/routing/traces/{id}` carries a `diagnosis`: one server-authored
sentence saying why the decision went this way and what to change, plus a
Jaccard near-miss ranking. `?expected_agent_id=` narrows it to "why was THIS
agent not a candidate", which is the question the tuning card is actually
opened to answer.

**Why every test here asserts the sentence, not just the code.** The wording
*is* the feature. A test that pinned only `code` would keep passing while the
sentence drifted into saying something false, and a wrong diagnosis about
somebody else's agent is worse than no diagnosis. So each test below pins the
exact text of the branch it exercises. That makes them deliberately brittle to
rewording, which is the intended trade: reword the verdict, update the test, and
the update is where somebody re-reads whether the new sentence is still true.

**The verdicts are split by origin** (`docs/plans/channel_routing_scope_split_plan.md`
§5, Phase 4), so this file is split the same way and the two halves are produced
differently — which is itself the thing worth understanding before editing here:

*Channel origins* (`server_channel`, and the `simulate`/`replay` of a channel
decision) are reached through a **real routing decision**: `POST
/admin/routing/simulate` runs the real router over the target's real state and
has no side effects. A verdict is a statement about what routing did, and a
fabricated trace would let it be right about a decision the router cannot
actually produce. Simulate rather than a webhook delivery because these branches
are about *routing state*, not delivery — same `ChannelRoutingService.decide`,
three fewer moving parts, no channel to set up. Where a branch needs
configuration that changed *after* the decision, the change is made between the
simulate and the read, which is the real-world shape of those branches too.

*App MCP origins* cannot be produced that way, and saying why is the point:
**nothing opens an `origin="app_mcp"` capture.** `ORIGIN_APP_MCP` is reserved
vocabulary (`routing_trace.py`) and routing_tuning Phase 6 owns emitting it, so
the only producer is `seed_routing_trace`, the documented Rule-1 exemption in
`tests/utils/routing.py`. Those tests therefore assert the *configuration*
branches only — the half whose answer comes from the database rather than from
the trace — and their candidate counts are all zero, because a seeded row has no
candidate list. The App MCP wording is unchanged by the scope split; these tests
exist to prove it stayed unchanged, not to re-derive it.

Branches that need a candidate list on a non-live origin, and skip reasons no
surface can produce any more (`identity_route`, `foreign_owner`, `agent_missing`
— see `routing_reachability_service`'s explanation tables), are pinned in
`tests/unit/test_routing_reachability.py`, which builds `RoutingDecisionPublic`
directly. Everything drivable is driven.
"""
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import (
    create_agent_via_api,
    set_router_trigger_prompt,
    update_agent,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_admin_route, create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.routing import (
    classification,
    get_routing_trace,
    patched_routing_externals,
    seed_routing_trace,
    simulate_routing,
)
from tests.utils.server_channel import add_auto_install_bundle
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


def _routable(
    client: TestClient, headers: dict[str, str], agents: list[dict]
) -> None:
    """Make each agent a **channel** candidate: give it a trigger prompt.

    This replaced `create_user_route` throughout the channel half. A personal
    `AppAgentRoute` grants nothing on the channel path any more — channel
    candidates are the sender's own agents, admitted on
    `router_trigger_prompt` or `example_prompts` and on nothing else
    (`ChannelCandidateProvider`).
    """
    for agent in agents:
        set_router_trigger_prompt(
            client, headers, agent["id"], f"Handle {agent['name']} work"
        )


def _simulate_no_match(
    client: TestClient, superuser_headers: dict[str, str], user_id: str, message: str
) -> dict:
    """A decision where the classifier ran and picked nothing.

    `classify_no_match` rather than leaving the classifier unpatched: there is
    no LLM in this environment, and an unpatched call would either reach a real
    provider or fail for a reason that has nothing to do with the branch under
    test. Note that a sender owning exactly one eligible agent, on a server with
    an empty auto-install list, takes Pass 1's `only_one` short-circuit and
    never reaches the classifier at all — so the no-match branches below give
    their sender two eligible agents, or none.
    """
    with patched_routing_externals(classify_no_match=True):
        return simulate_routing(
            client, superuser_headers, message=message, as_user_id=user_id
        )


def _seed_app_mcp_trace(user_id: str) -> dict:
    """A stored `origin="app_mcp"` decision, which nothing else can produce.

    See the module docstring: the App MCP capture is unbuilt (routing_tuning
    Phase 6), so the reserved origin has no live producer and this documented
    Rule-1 exemption is the only way to exercise the App MCP verdict half at
    all. The row carries no candidates, which is why every App MCP sentence
    below counts zero effective routes.
    """
    trace_id = seed_routing_trace(
        created_at=datetime.now(UTC),
        origin="app_mcp",
        user_id=user_id,
        outcome="no_match",
        message="please do the thing",
    )
    assert trace_id is not None, "seeded trace was not persisted"
    return {"id": str(trace_id)}


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
# General verdicts on a channel decision — no expected agent named
# ---------------------------------------------------------------------------


def test_verdict_when_the_user_has_no_candidates_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A user who owns nothing, with an empty auto-install list.

    The most common shape of "the bot didn't find my agent" on a fresh
    deployment, and the one an empty candidate table cannot explain on its own.
    The remedy names the Configuration tab, not an App MCP route: a channel
    reads no route at all (plan §2.4).
    """
    user, _ = _user(client, superuser_token_headers)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    assert trace["outcome"] == "no_match", trace

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider and no auto-install bundle was eligible, so "
        "no message from them can route anywhere. Set a router trigger prompt "
        "(or example prompts) on the agent you expected, from its "
        "Configuration tab, or add its bundle to the auto-install list."
    )
    assert diagnosis["eligible_candidate_count"] == 0
    # The remedy is a substring of the sentence by construction, so a client
    # rendering them separately cannot show two different answers.
    assert diagnosis["action"] in diagnosis["verdict"]


def test_verdict_when_owned_agents_exist_but_the_classifier_matched_none(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Three eligible owned agents, classifier picks nothing.

    The counted noun is the diagnosis, not decoration: "3 effective routes"
    would send an admin to a routes list to look for three rows that need not
    exist, because a channel candidate has no route behind it.
    """
    user, headers = _user(client, superuser_token_headers)
    agents = [_agent(client, headers, f"NoMatch{i}") for i in range(3)]
    _routable(client, headers, agents)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "solve this equation"
    )
    diagnosis = _diagnosis(client, superuser_token_headers, trace)

    assert diagnosis["code"] == "no_match"
    assert diagnosis["eligible_candidate_count"] == 3
    assert diagnosis["verdict"] == (
        "This user has 3 eligible candidates and the classifier matched none "
        "of them. Widen the trigger prompt of the agent that should have won — "
        "the near-miss scores below say which came closest — or use Draft a "
        "recommendation to generate wording for its owner."
    )


def test_verdict_when_every_candidate_was_excluded_before_the_classifier(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """One owned agent with no router wording at all.

    It is still recorded as a candidate (with `no_trigger_prompt`) rather than
    dropped, which is the only reason this branch can say "excluded" instead of
    "you have nothing" — and the whole reason the reported incident needed a
    database query to diagnose.
    """
    user, headers = _user(client, superuser_token_headers)
    _agent(client, headers, "Wordless")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please handle it"
    )
    diagnosis = _diagnosis(client, superuser_token_headers, trace)

    assert diagnosis["code"] == "all_candidates_skipped"
    assert diagnosis["skipped_by_reason"] == {"no_trigger_prompt": 1}
    assert diagnosis["verdict"] == (
        "This user has no eligible candidates: 1 candidate was excluded before "
        "the classifier saw it (no_trigger_prompt). Fix the exclusion on the "
        "agent you expected — the candidate table below names the reason for "
        "each one."
    )


def test_verdict_when_the_decision_routed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A success is a verdict too — and it must not read as a problem."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Winner")
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    with patched_routing_externals(classify_result=classification(agent["id"])):
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

    Origin-neutral on purpose: this sentence names no route and no trigger
    prompt, so there is nothing in it for the surface split to change.

    The failing pass persists its own trace before re-raising, and simulate
    reports the failure with a 500 that names where to find it — so the row is
    fetched from the trace list rather than from the simulate response.
    """
    from tests.utils.routing import list_routing_traces

    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Boom")
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

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
# Channel expected-agent verdicts answered from the trace
# ---------------------------------------------------------------------------


def test_verdict_when_the_expected_agent_was_the_one_chosen(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Chosen")
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    with patched_routing_externals(classify_result=classification(agent["id"])):
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
    set_router_trigger_prompt(
        client, headers, agents[0]["id"], "eigenvalue matrix questions"
    )
    set_router_trigger_prompt(
        client, headers, agents[1]["id"], "calendar booking requests"
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


def test_verdict_when_an_owned_agent_has_no_router_wording(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """**The live path** for "why wasn't my own agent considered".

    Worth being explicit about where this sentence comes from, because the
    obvious guess is wrong. The candidate provider records a wording-less owned
    agent as a *skipped candidate* rather than dropping it, so the trace has a
    row for it and `_verdict_from_trace` answers first — this is the
    skip-explanation override firing, not the configuration branch. A
    channel-configuration branch keyed on "the trace never mentions it" would
    never run for this case at all.

    The remedy is the whole §2.4 correction: the Configuration tab, not the
    Integrations tab, and it names example prompts as the second way in.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "NoWording")

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please handle it"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_skipped"
    assert diagnosis["verdict"] == (
        f"{agent['name']} was considered for this decision and then excluded: "
        f"it has neither a router trigger prompt nor example prompts, so the "
        f"classifier had nothing to match the message against. Set a router "
        f"trigger prompt (or example prompts) on the agent's Configuration tab."
    )
    assert "App MCP" not in diagnosis["verdict"]


def test_verdict_for_an_already_installed_auto_install_bundle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Pass 2's `already_installed` skip, on a channel — the §2.4 defect's last
    live instance.

    Its remedy used to read *"Check the installed agent's App MCP route"*, and
    it is produced by the auto-install scan as a `KIND_BUNDLE` candidate. That
    is why the override is gated on the **origin** and not on the candidate
    kind: a `kind == "agent"` gate looks equivalent and would have walked
    straight past the one remedy on a channel decision that still pointed at an
    MCP control.
    """
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    source = _agent(client, publisher_headers, "Installed")
    set_router_trigger_prompt(
        client, publisher_headers, source["id"], "Handle bundle routing questions"
    )
    publish_bundle_and_make_public(client, publisher_headers, source["id"])
    published = client.get(
        f"{API}/agents/{source['id']}", headers=publisher_headers
    ).json()
    # Two different identifiers, and the install route wants the other one: the
    # auto-install list and the trace's `ref_id` are keyed by the bundle's
    # UUID, while `POST /catalog/{bundle_id}/install` takes the reverse-DNS id.
    bundle_uuid = str(published["bundle_uuid"])
    bundle_id = published["bundle_id"]

    listing = add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    display_name = next(
        row["display_name"] for row in listing if row["bundle_uuid"] == bundle_uuid
    )

    consumer, consumer_headers = _user(client, superuser_token_headers)
    install_bundle(client, consumer_headers, bundle_id)

    # The install carries the trigger prompt, so Pass 1 has a real ballot and
    # genuinely classifies before Pass 2's scan runs.
    trace = _simulate_no_match(
        client, superuser_token_headers, consumer["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=bundle_uuid
    )

    assert diagnosis["code"] == "expected_agent_skipped"
    assert diagnosis["verdict"] == (
        f"{display_name} was considered for this decision and then excluded: "
        f"this user already has it installed, so the auto-install pass passed "
        f"over it — it should have been reachable in Pass 1 as one of the "
        f"agents they own instead. Set a router trigger prompt (or example "
        f"prompts) on the installed agent's Configuration tab: an install with "
        f"neither is not a channel candidate, which is exactly this gap."
    )
    assert "App MCP" not in diagnosis["verdict"]


# ---------------------------------------------------------------------------
# Channel expected-agent verdicts answered from current configuration
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


def test_verdict_for_an_agent_created_after_the_decision_with_no_wording(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The configuration branch's own no-wording finding, which is narrower than
    it looks: the agent has to be one the decision never saw.

    Created *after* the simulate, so the trace has no row for it — an agent
    owned at capture time would have been recorded as a `no_trigger_prompt`
    skip and answered from the trace instead. Distinct code from the App MCP
    `expected_agent_no_trigger_prompt`, whose finding is about an install's
    auto-route never having been created.
    """
    user, headers = _user(client, superuser_token_headers)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    agent = _agent(client, headers, "Later")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_channel_no_trigger_prompt"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {agent['name']} is not among "
        f"them because it has neither a router trigger prompt nor example "
        f"prompts, so there is nothing for the classifier to match a message "
        f"against. Set a router trigger prompt (or example prompts) on the "
        f"agent's Configuration tab. (An App MCP route is not part of this: "
        f"channel routing reads no route, no assignment and no App MCP toggle.)"
    )


def test_verdict_for_a_foreign_agent_on_a_channel_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Never a candidate, and the reason is ownership rather than a switch.

    The channel remedy says nothing about assigning an App MCP route — on this
    surface that would change nothing, however correct it is on the other one.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")
    set_router_trigger_prompt(client, owner_headers, agent["id"], "Handle anything")

    trace = _simulate_no_match(
        client, superuser_token_headers, sender["id"], "please do the thing"
    )
    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )

    assert diagnosis["code"] == "expected_agent_foreign_owner"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {agent['name']} is not among "
        f"them because it belongs to a different account, and a channel routes "
        f"only over the sender's own agents. Share its bundle with this user "
        f"and have them install it — a channel session runs on the sender's "
        f"own install, so the install they own is the only thing a channel can "
        f"reach."
    )
    assert diagnosis["expected_agent_owner_email"] == owner["email"]


def test_verdict_when_the_agent_was_given_a_trigger_prompt_after_the_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The one branch whose remedy is "re-run", not "change something".

    Without it, an admin who has just fixed the wording would read a stale
    trace as evidence the fix did not work. On a channel the promise is honest:
    a replay re-runs `ChannelRoutingService.decide`, which is the same pass that
    produced this trace.
    """
    user, headers = _user(client, superuser_token_headers)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    agent = _agent(client, headers, "FixedSince")
    set_router_trigger_prompt(client, headers, agent["id"], "Handle it")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_looks_reachable"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {agent['name']} is not among "
        f"them because it was not a candidate when this decision ran, even "
        f"though this user owns it and its router trigger prompt or example "
        f"prompts are set now. Re-run this decision — an agent created, "
        f"transferred or given wording after the trace was captured explains "
        f"exactly this, and the re-run will show it as a candidate."
    )


def test_example_prompts_alone_count_as_router_wording(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`example_prompts` is the second way an agent becomes reachable, and the
    verdict has to agree with the candidate provider about that.

    Same setup as the branch above with the trigger prompt left blank: if this
    module ever grew its own copy of the eligibility rule instead of calling
    `ChannelCandidateProvider`'s, the two would disagree here first — the card
    would report "it has neither" about an agent the provider was admitting.
    """
    user, headers = _user(client, superuser_token_headers)

    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    agent = _agent(client, headers, "ExamplesOnly")
    update_agent(
        client, headers, agent["id"], example_prompts=["restart the payment worker"]
    )

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_looks_reachable", diagnosis


# ---------------------------------------------------------------------------
# App MCP verdicts — unchanged wording, on a seeded `app_mcp` trace
#
# See the module docstring: nothing opens an App MCP capture, so these are the
# configuration branches only, on rows with no candidate list.
# ---------------------------------------------------------------------------


def test_app_mcp_verdict_when_the_user_has_no_candidates_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The App MCP half of `no_candidates` — the wording every origin used to
    get, kept verbatim for the surface it is actually true of."""
    user, _ = _user(client, superuser_token_headers)
    trace = _seed_app_mcp_trace(user["id"])

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: no App MCP route reaches "
        "them and no auto-install bundle was eligible, so no message from them "
        "can route anywhere. Give the agent you expected an App MCP route from "
        "its Integrations tab, or add its bundle to the auto-install list."
    )


def test_app_mcp_verdict_for_a_standalone_agent_with_no_app_mcp_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`_create_auto_route_for_agent` returns `None` when `bundle_uuid IS NULL`,
    so a standalone agent never gets an automatic route and is absent from
    `get_effective_routes_for_user`.

    Still true, still deliberate, and still the right thing to say — on the App
    MCP surface. It is what channel routing no longer says.
    """
    user, headers = _user(client, superuser_token_headers)
    orphan = _agent(client, headers, "Standalone")
    trace = _seed_app_mcp_trace(user["id"])

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=orphan["id"]
    )
    assert diagnosis["code"] == "expected_agent_standalone_no_route"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {orphan['name']} is not among them "
        f"because it is not a bundle install and has no App MCP route. Add one "
        f"from its Integrations tab. (Setting its router trigger prompt alone "
        f"will not do it: a standalone agent never gets an automatic route — "
        f"that is deliberate, its owner manages App MCP exposure explicitly.)"
    )


def test_app_mcp_verdict_for_a_bundle_install_whose_revision_has_no_trigger_prompt(
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
    trace = _seed_app_mcp_trace(consumer["id"])

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


def test_app_mcp_verdict_for_a_bundle_install_whose_route_was_deleted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Trigger prompt set, route gone — a different repair from the no-prompt one."""
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    source = _agent(client, publisher_headers, "HasPrompt")
    set_router_trigger_prompt(
        client, publisher_headers, source["id"], "Handle bundle routing questions"
    )
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

    trace = _seed_app_mcp_trace(consumer["id"])
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


def test_app_mcp_verdict_for_a_foreign_agent_with_no_route_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """On App MCP, ownership is not the last word — a route can assign somebody
    else's agent to this user, so the remedy names that as an option."""
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")
    trace = _seed_app_mcp_trace(sender["id"])

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


def test_app_mcp_verdict_when_the_route_exists_but_is_not_assigned_to_this_user(
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
    trace = _seed_app_mcp_trace(user["id"])

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


def test_app_mcp_verdict_when_the_route_is_not_enabled_for_the_app_mcp_channel(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An active route that is off the App MCP channel."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "WrongChannel")
    create_user_route(
        client,
        headers,
        agent["id"],
        trigger_prompt="Handle it",
        channel_app_mcp=False,
    )
    trace = _seed_app_mcp_trace(user["id"])

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


def test_app_mcp_verdict_when_the_route_is_switched_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The configuration branch for an inactive route."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "LaterOff")
    create_user_route(
        client, headers, agent["id"], trigger_prompt="Handle it", is_active=False
    )
    trace = _seed_app_mcp_trace(user["id"])

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_route_inactive"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because its App MCP route exists but is switched off. Switch the "
        f"route back on from the agent's Integrations tab."
    )


def test_app_mcp_verdict_when_the_route_was_added_after_the_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """"It looks fine now" — and the remedy no longer promises a replay proves it.

    Replay re-runs `ChannelRoutingService.decide`, which since the scope split
    builds its ballot from the sender's own agents and reads no route at all. So
    a replay of an App MCP trace confirms nothing about the route, and the old
    wording ("the re-run will show it as a candidate") would have had an admin
    conclude their fix had not taken.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "FixedSince")
    trace = _seed_app_mcp_trace(user["id"])
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle it")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_looks_reachable"
    assert diagnosis["verdict"] == (
        f"This user has 0 effective routes; {agent['name']} is not among them "
        f"because it was not a candidate when this decision ran, even though "
        f"its App MCP route looks correctly configured now. Confirm it from "
        f"the route's own page — a route added or switched on after the trace "
        f"was captured explains exactly this. Replay will not show it: a "
        f"replay re-runs the channel pass, which reads no App MCP route."
    )


# ---------------------------------------------------------------------------
# Near-miss ranking (auto_routing_tuning plan §3 — the Jaccard helpers, reused)
# ---------------------------------------------------------------------------


def test_near_misses_are_ranked_best_first_against_the_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """"closest: Equation Assistant 0.31" — the §9 output, ranked server-side.

    Two owned agents whose trigger prompts share very different amounts of
    vocabulary with the message. Asserting the *order* rather than a hard-coded
    score: the ranking is what the card reads, and pinning exact Jaccard values
    here would turn a tuning change to the shared tokenizer into a failure in
    the wrong file.
    """
    user, headers = _user(client, superuser_token_headers)
    close = _agent(client, headers, "Equation")
    far = _agent(client, headers, "Calendar")
    set_router_trigger_prompt(
        client, headers, close["id"], "eigenvalue matrix questions"
    )
    set_router_trigger_prompt(
        client, headers, far["id"], "booking holiday travel plans"
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
    set_router_trigger_prompt(
        client, headers, agent["id"], "eigenvalue matrix questions"
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
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    with patched_routing_externals(classify_result=classification(agent["id"])):
        simulated = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=user["id"],
        )
    fetched = get_routing_trace(client, superuser_token_headers, simulated["id"])

    assert simulated["diagnosis"] is not None
    assert simulated["diagnosis"] == fetched["diagnosis"]
