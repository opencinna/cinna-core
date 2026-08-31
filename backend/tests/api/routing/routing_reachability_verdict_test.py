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

**The verdicts are split by origin** (`docs/application/routing_tuning/routing_tuning.md`
— *the verdict is split by origin for wording, never for findings*), so this
file is split the same way and the two halves are produced
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

*App MCP origins* are **seeded**, not simulated, and saying why is the point.
`AppMCPRoutingService.route_message` does open an `origin="app_mcp"` capture
now (phase 6 of `docs/plans/channels_identity_unification/` — this file used to
say nothing did, and that stopped being true), but simulate still cannot
produce one: `POST /admin/routing/simulate` runs the *channel* router. Reaching
a real App MCP decision from here would mean calling the MCP handler and
standing up a ballot, for branches whose answer comes from the database rather
than from the trace. So these use `seed_routing_trace`, the documented Rule-1
exemption in `tests/utils/routing.py`, and their candidate counts are all zero
because a seeded row has no candidate list. That the App MCP *capture* now
writes real rows — with a populated `channel_id`, under
`ROUTING_TRACE_APP_MCP_MODE` — is proved in
`tests/api/app_mcp/app_mcp_routing_trace_test.py`, not here. The App MCP
wording is unchanged by the scope split; these tests exist to prove it stayed
unchanged, not to re-derive it.

A third section, at the bottom, covers the remedy **profiles** themselves —
the generic arm an unmapped origin degrades to, and email's mapping onto the
channel arm. Both are seeded for the same reason App MCP is, and both are
failures that would otherwise be invisible: see that section's own header.

Branches that need a candidate list on a non-live origin, and skip reasons no
surface can produce any more (`identity_route`, `foreign_owner`, `agent_missing`
— see `routing_reachability_service`'s explanation tables), are pinned in
`tests/unit/test_routing_reachability.py`, which builds `RoutingDecisionPublic`
directly. Everything drivable is driven.
"""
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import ROUTING_TRACE_APP_MCP_FULL, settings
from app.models import ChannelUserSetting
from tests.utils.agent import (
    create_agent_via_api,
    set_router_trigger_prompt,
    update_agent,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.identity import share_identity_agent
from tests.utils.routing import (
    classification,
    get_routing_trace,
    list_routing_traces,
    patched_routing_externals,
    post_channel_message,
    seed_routing_trace,
    simulate_routing,
)
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
    update_server_channel,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import update_my_channel
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
#: Pinned by `_seed_app_mcp_trace` so the seeded instrument does not inherit
#: the deployed default of the per-origin App MCP write mode.
_APP_MCP_MODE_SETTING = "app.core.config.settings.ROUTING_TRACE_APP_MCP_MODE"


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
    """A stored `origin="app_mcp"` decision with a controlled shape.

    App MCP *does* open a capture now (phase 6), but driving one here would
    buy nothing and cost determinism: these tests are about the verdict's
    **configuration** branches, whose answer comes from the database rather
    than from the trace, and a real App MCP decision would drag an MCP handler
    call and a candidate ballot in with it. The row carries no candidates,
    which is why every App MCP sentence below counts "0 eligible candidates" —
    the same noun a channel decision uses; the per-origin count noun ("N
    effective routes") was deleted with the AppAgentRoute family in phase 5.

    `ROUTING_TRACE_APP_MCP_MODE` is pinned to `"full"` across the seed so the
    instrument stays deterministic whatever the deployed default is. Without
    it this helper would inherit the metadata default and start writing rows
    with no `message_text` — the flag reaching an instrument it was never
    aimed at. Nothing below reads `message_text`; the patch is here so that
    stays a fact about the helper rather than a coincidence.
    """
    with patch(_APP_MCP_MODE_SETTING, ROUTING_TRACE_APP_MCP_FULL):
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


def _no_candidate_channel(
    client: TestClient,
    superuser_headers: dict[str, str],
    **admin_defaults: object,
) -> dict:
    """A channel any sender may reach, with the given admin-owned defaults."""
    channel = create_server_channel(
        client, superuser_headers, auto_register_users=True, email_whitelist="*"
    )
    if admin_defaults:
        update_server_channel(
            client, superuser_headers, channel["id"], **admin_defaults
        )
    return channel


def _deliver_from_a_sender_who_owns_nothing(
    client: TestClient,
    superuser_headers: dict[str, str],
    channel: dict,
    *,
    sender_email: str | None = None,
) -> dict:
    """One real webhook delivery, and the `no_match` trace it leaves.

    Driven through the **webhook**, not through simulate, and that is forced
    rather than stylistic: a channel-policy verdict needs a decision that
    genuinely belonged to a channel *and* was produced by the real inbound
    path. `RoutingSimulateRequest` can now name a `channel_id`, so the first
    half is no longer simulate's to fail — but the second half still is. These
    verdicts are about what an actual delivery left behind: a real sender, a
    real thread, a real 200 back to Google Chat. A simulate names a channel
    while deciding for nobody in particular, with no delivery underneath it.

    No classifier answer is named, on purpose: the sender owns nothing, so the
    ballot is empty and `post_channel_message`'s stub raises if it is ever
    reached (see its docstring for the two shapes allowed to name none).
    """
    signer = GoogleChatJWTSigner()
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="please do the thing",
        sender_email=sender_email or f"{random_lower_string()}@example.com",
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1, page
    trace = page["data"][0]
    assert trace["outcome"] == "no_match", trace
    return trace


def _set_sender_scope(db: Session, channel_id: str, user_id: str, scope: str) -> None:
    """Give this sender their OWN `channel_user_setting`, with `agent_scope` set.

    Written through the session rather than through
    `PUT /users/me/channels/{channel_id}` — which is the only production creator
    of this row — as a focused setup shortcut. What this test needs from the row
    is one bit, that `agent_scope` is the SENDER'S and not inherited, and the row
    is exactly what expresses it; the route itself is covered in
    `tests/api/server_channels/server_channels_user_settings_test.py`.

    This used to be a workaround rather than a choice: while `_create_setting`
    wrapped its insert in `session.begin_nested()`, the inner savepoint's commit
    fired `after_transaction_end` inside the suite's `restart_savepoint` listener
    and the route could not create a row under these fixtures at all. The insert
    is a native upsert now, so the shortcut is a preference again.
    """
    setting = ChannelUserSetting(
        server_channel_id=uuid.UUID(channel_id),
        user_id=uuid.UUID(user_id),
        agent_scope=scope,
    )
    db.add(setting)
    db.commit()


def test_verdict_when_no_candidates_and_the_sender_restricted_the_scope(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """`no_candidates` split by why Pass 2 never ran — the sender's own scope.

    The base sentence ends "…or add its bundle to the auto-install list", and on
    a scope-restricted channel that names a control which would change nothing:
    `_catalog_may_run` gates Pass 2 on `agent_scope == "all"`, because an agent
    arriving from the catalog is out of the chosen set by construction.

    Reported ahead of the auto-install half when both bar the pass, because a
    restricted scope also invalidates the *other* remedy — a newly created agent
    would be outside the chosen set too.
    """
    channel = _no_candidate_channel(client, superuser_token_headers)
    sender, _ = create_random_user_with_headers(client)
    _set_sender_scope(db, channel["id"], sender["id"], "list")

    trace = _deliver_from_a_sender_who_owns_nothing(
        client, superuser_token_headers, channel, sender_email=sender["email"]
    )

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates_channel_scope"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider, and the auto-install pass could not offer "
        "them one either — as this channel's settings stand right now, this "
        "sender has limited it to an explicitly chosen set of their own "
        "agents, and an agent installed from the catalog would not be in that "
        "set. Set this channel back to using every agent they own, in their "
        "Settings > Channels, and then give them an agent with a router "
        "trigger prompt (or example prompts). Nothing on an agent alone will "
        "help while the limit stands: a newly created agent would be outside "
        "the chosen set too. Note that this names the channel's settings as "
        "they stand right now, not as they stood when the decision ran — a "
        "verdict answers what to change today."
    )
    assert diagnosis["action"] in diagnosis["verdict"]


def test_verdict_when_no_candidates_and_the_admin_default_restricts_the_scope(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The same finding, the other owner — and the reason this pair exists.

    `agent_scope` **inherits**: a sender with no `channel_user_setting` row
    follows `channel.default_agent_scope`, which is the normal state for the
    auto-registered senders this whole feature is built for. Telling a superuser
    that "this sender has restricted it" would blame an external Google Chat
    user, who may have no account UI at all, for an admin's default — and send
    the reader to a screen where nothing is set.

    Same code as the test above (the finding is identical); a different remedy,
    because the control lives elsewhere.
    """
    channel = _no_candidate_channel(
        client, superuser_token_headers, default_agent_scope="none"
    )
    trace = _deliver_from_a_sender_who_owns_nothing(
        client, superuser_token_headers, channel
    )

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates_channel_scope"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider, and the auto-install pass could not offer "
        "them one either — as this channel's settings stand right now, its "
        "admin default limits every sender to an explicitly chosen set of "
        "their own agents, and an agent installed from the catalog would not "
        "be in that set. This sender has set nothing of their own, so they "
        "follow that default. Set this channel's default agent scope back to "
        "every agent a user owns, in its admin settings — there is nothing to "
        "change on this sender's side, because they have overridden nothing. "
        "Nothing on an agent alone will help while the default stands: a newly "
        "created agent would be outside the chosen set too. Note that this "
        "names the channel's settings as they stand right now, not as they "
        "stood when the decision ran — a verdict answers what to change today."
    )
    assert diagnosis["action"] in diagnosis["verdict"]


def test_verdict_when_no_candidates_and_auto_install_is_switched_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`no_candidates`, split by why Pass 2 never ran — the auto-install half.

    The defect this branch fixes: with `allow_auto_install=False` the base
    sentence advised adding a bundle to the auto-install list, a list that is
    never read while the switch is off.

    Note the verdict is computed from the channel policy **as it stands when
    the trace is read**, which is stated in the sentence itself rather than
    left as a silent assumption — see `_channel_pass_2_block`.
    """
    channel = _no_candidate_channel(
        client, superuser_token_headers, allow_auto_install=False
    )
    trace = _deliver_from_a_sender_who_owns_nothing(
        client, superuser_token_headers, channel
    )

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates_auto_install_off"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider, and the auto-install pass never ran — as "
        "this channel's settings stand right now, installing a bundle for its "
        "senders is switched off. Give this user an agent with a router "
        "trigger prompt (or example prompts) on its Configuration tab, or "
        "switch auto-installing bundles back on for this channel in its admin "
        "settings. Adding a bundle to the auto-install list will not help on "
        "its own — while that switch is off the list is never read. Note that "
        "this names the channel's settings as they stand right now, not as "
        "they stood when the decision ran — a verdict answers what to change "
        "today."
    )
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


# ---------------------------------------------------------------------------
# The expected candidate is a person, not an agent
# ---------------------------------------------------------------------------


def test_verdict_for_an_identity_owner_who_shared_nothing_reachable(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`?expected_agent_id=identity:{owner_id}` — "why was this PERSON not
    reachable", which the surface could not phrase until phase 6 widened the
    parameter from `uuid.UUID` to `str`.

    `SKIP_IDENTITY_UNAVAILABLE` has been a live channel row since phase 3 — an
    identity owner named this sender on a binding, the sender never switched
    the contact on, so nothing they shared was reachable and they never made
    the ballot. What was *not* reachable until now is the sentence explaining
    it: `_verdict_from_trace` renders a skip explanation only on this branch,
    and no UUID can name a candidate whose ref is `identity:{owner_id}`. This
    is the first test to reach that copy, which is why
    `routing_reachability_service`'s two `SKIP_IDENTITY_UNAVAILABLE` entries
    now name it.

    Driven through a **real webhook delivery** rather than simulate, for the
    reason `tests/api/server_channels/server_channels_identity_trace_test.py`
    gives at length: identity needs a channel policy with
    `allow_identity_routing` on, and the row asserted here has exactly one live
    producer, which is the inbound path.

    The last assertion is the one that says *which* of the two entries was
    served. The base table's remedy sends the reader to "the Identity Contacts
    section of the MCP Server card"; the channel override sends them to the
    owner's Identity Server card instead, because a channel reads no MCP
    control at all. Both entries carry the same finding, so pinning only the
    finding would pass on either.
    """
    channel = create_server_channel(
        client, superuser_token_headers, auto_register_users=False,
        email_whitelist="*",
    )
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _user(client, superuser_token_headers)
    hr_agent = _agent(client, owner_headers, "HRUnreachable")
    sender, sender_headers = create_random_user_with_headers(client)
    share_identity_agent(
        client,
        owner_headers,
        sender_headers,
        agent_id=hr_agent["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Answer time-off questions",
        # Never switched on by the sender, so the assignment stays
        # `is_enabled=False` — the state recorded as this skip.
        enable=False,
    )
    update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )

    # No classifier answer is named: the only candidate was skipped, so the
    # ballot is empty and the decision short-circuits before the classifier.
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text="hey, ask HR what is my time-off status?",
        sender_email=sender["email"],
    )
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200

    page = list_routing_traces(
        client, superuser_token_headers, channel_id=channel["id"]
    )
    assert page["count"] == 1, page
    trace = page["data"][0]

    diagnosis = _diagnosis(
        client,
        superuser_token_headers,
        trace,
        expected_agent_id=f"identity:{owner['id']}",
    )

    assert diagnosis["code"] == "expected_agent_skipped", diagnosis
    # Echoed back verbatim: the ref the caller asked about, not a UUID it was
    # coerced into.
    assert diagnosis["expected_agent_id"] == f"identity:{owner['id']}", diagnosis
    # The candidate row's own label and owner — the person, since no `Agent`
    # row was ever looked up for this ref.
    assert diagnosis["expected_agent_name"] == owner["email"], diagnosis
    assert diagnosis["expected_agent_owner_email"] == owner["email"], diagnosis
    assert diagnosis["verdict"] == (
        f"{owner['email']} was considered for this decision and then "
        f"excluded: this person had named the sender on an identity binding, "
        f"so they were recorded on this decision, but nothing they shared was "
        f"reachable when the message arrived — they were on the trace and "
        f"never on the ballot. The switch that fixes this is the identity "
        f"owner's, not the sender's, in two of the three cases: on the "
        f"owner's Settings > Channels > Identity Server card, either the "
        f"binding itself is inactive or this sender's assignment to it is. "
        f"The third is the sender's own contact toggle for that person, in "
        f"their Settings > Channels. Check the owner's two first — a sender "
        f"cannot enable a contact nobody has shared with them. What this is "
        f"NOT is the sender's channel-level identity-routing switch: with "
        f"that off, no identity appears on a channel trace at all, so this "
        f"row is evidence it was already on."
    ), diagnosis
    assert diagnosis["action"] in diagnosis["verdict"], diagnosis
    # The channel override, not the App-MCP-voiced base entry.
    assert "MCP Server card" not in diagnosis["verdict"], diagnosis


def test_verdict_for_an_identity_ref_nobody_on_this_decision_matches(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A well-formed `identity:` ref that names no candidate — the guard.

    This is the branch that would have failed *silently*. `expected_agent_id`
    reaches `db.get(Agent, ...)`, and with the parameter widened to `str` an
    unguarded lookup raises on `identity:{uuid}`, gets swallowed by
    `RoutingReachabilityService.diagnose`'s total guard, and comes back as
    `unavailable` — "this decision's diagnosis could not be computed", which on
    the tuning card reads as though the question had never been asked. So the
    parse is explicit (`_agent_uuid`) and this test pins the answer rather than
    the absence of a crash: the ref names nobody, and the verdict says so in
    ref-shaped words instead of claiming "no agent identity:… exists on this
    server", which would be false about a ref that never named an agent.

    Same code as the bare-UUID miss on purpose — the finding is identical, and
    a new wire value would oblige every client to render it to say nothing new.
    """
    user, _ = _user(client, superuser_token_headers)
    trace = _simulate_no_match(
        client, superuser_token_headers, user["id"], "please do the thing"
    )
    stranger = f"identity:{uuid.uuid4()}"

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=stranger
    )

    assert diagnosis["code"] == "expected_agent_unknown", diagnosis
    assert diagnosis["verdict"] == (
        f"No candidate {stranger} appears on this decision, so there is "
        f"nothing recorded here to explain about it. Check the ref against "
        f"the candidate table below — an identity candidate is named "
        f"identity: followed by the owner's user id, and a person nobody "
        f"recorded has no row on this trace at all."
    ), diagnosis
    assert diagnosis["expected_agent_id"] == stranger, diagnosis
    assert diagnosis["expected_agent_name"] is None, diagnosis


def test_verdict_for_an_agent_created_after_the_decision_with_no_wording(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The configuration branch's own no-wording finding, which is narrower than
    it looks: the agent has to be one the decision never saw.

    Created *after* the simulate, so the trace has no row for it — an agent
    owned at capture time would have been recorded as a `no_trigger_prompt`
    skip and answered from the trace instead. Since phase 5 of
    `docs/plans/channels_identity_unification/`, this is no longer a
    channel-only code: `_verdict_from_configuration` collapsed the channel and
    App MCP configuration branches into one function ("who owns it, and
    whether its owner wrote anything for the classifier to match on" is the
    same question on both surfaces now that the `AppAgentRoute` family is
    gone), so `expected_agent_channel_no_trigger_prompt` — the name is
    historical — fires with the same wording for an App MCP decision too; see
    `test_app_mcp_verdict_for_an_owned_agent_with_no_router_wording`.
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
        f"agent's Configuration tab. That pair is the whole of it — there is "
        f"no route, assignment or per-agent toggle to configure anywhere."
    )


def test_verdict_for_a_foreign_agent_on_a_channel_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Never a candidate, and the reason is ownership rather than a switch.

    The remedy says nothing about assigning an App MCP route — that
    mechanism is deleted — and, since phase 5's collapse of
    `_verdict_from_configuration` into one function for both origins, the
    finding and remedy are now identical in shape to App MCP's own foreign-
    owner verdict; see
    `test_app_mcp_verdict_for_a_foreign_agent_with_no_grant`. Only the one
    `{surface}` clause differs.
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
        f"them because it belongs to a different account, and a channel "
        f"routes over the caller's own agents. Share its bundle with this "
        f"user and have them install it — the session runs on the caller's "
        f"own install, so the install they own is the only thing this "
        f"surface can reach. (Reaching somebody else's agent is what "
        f"identity contacts are for, and that is a different question from "
        f"this one.)"
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
# App MCP verdicts, on a seeded `app_mcp` trace
#
# See the module docstring: nothing opens an App MCP capture, so these are the
# configuration branches only, on rows with no candidate list.
#
# Phase 5 of docs/plans/channels_identity_unification/ deleted the six
# route-based verdict codes this section used to test one-by-one
# (CODE_EXPECTED_STANDALONE_NO_ROUTE, _BUNDLE_NO_ROUTE, _NO_TRIGGER_PROMPT,
# _ROUTE_INACTIVE, _ROUTE_NOT_APP_MCP, _ROUTE_UNASSIGNED) along with the
# AppAgentRoute family whose configuration they described. What is left is
# `_verdict_from_configuration`'s new, origin-neutral shape: App MCP asks the
# same two questions a channel does — who owns the agent, and did its owner
# write anything for the classifier to match on — so this section shrank to
# the App MCP half of the three surviving shared branches, each cross-
# referenced against its channel-origin twin above.
# ---------------------------------------------------------------------------


def test_app_mcp_verdict_when_the_user_has_no_candidates_at_all(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """App MCP's `no_candidates` sentence, now the same shape a channel's
    non-scoped `no_candidates` gets — no route or Integrations-tab wording,
    because App MCP builds its ballot from `ChannelCandidateProvider` exactly
    as a channel does and has no Pass 2 catalog of its own to name either."""
    user, _ = _user(client, superuser_token_headers)
    trace = _seed_app_mcp_trace(user["id"])

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider, so no message from them can route "
        "anywhere. Give this user an agent with a router trigger prompt (or "
        "example prompts) on its Configuration tab."
    )


def test_app_mcp_verdict_for_an_owned_agent_with_no_router_wording(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The App MCP half of `expected_agent_channel_no_trigger_prompt` — the
    "channel" in the code name is historical (settled decision, plan §2.3):
    the same code and the same wording now fire on an App MCP decision, since
    `_verdict_from_configuration` collapsed both origins' no-wording finding
    into one branch. See
    `test_verdict_for_an_agent_created_after_the_decision_with_no_wording`
    for the channel-origin twin — the two differ only in how the trace was
    produced (simulate vs. `_seed_app_mcp_trace`), not in wording.

    Replaces the old `..._for_a_bundle_install_whose_revision_has_no_trigger_
    prompt`: a bundle install's auto-created route is deleted machinery, and
    an owned agent needs nothing route-shaped to reach this branch any more —
    just no trigger prompt and no example prompts, exactly like the channel
    case.
    """
    user, headers = _user(client, superuser_token_headers)
    orphan = _agent(client, headers, "NoWording")
    trace = _seed_app_mcp_trace(user["id"])

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=orphan["id"]
    )
    assert diagnosis["code"] == "expected_agent_channel_no_trigger_prompt"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {orphan['name']} is not among "
        f"them because it has neither a router trigger prompt nor example "
        f"prompts, so there is nothing for the classifier to match a message "
        f"against. Set a router trigger prompt (or example prompts) on the "
        f"agent's Configuration tab. That pair is the whole of it — there is "
        f"no route, assignment or per-agent toggle to configure anywhere."
    )


def test_app_mcp_verdict_for_a_foreign_agent_with_no_grant(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The App MCP half of `expected_agent_foreign_owner` — since the route
    family that used to let App MCP reach somebody else's agent is deleted,
    this finding and remedy are now identical in shape to the channel-origin
    twin (`test_verdict_for_a_foreign_agent_on_a_channel_decision`); only the
    `{surface}` clause names "App MCP" instead of "a channel". Reaching a
    foreign owner's agent is an identity-contact question now, on both
    surfaces, and the remedy says so.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")
    trace = _seed_app_mcp_trace(sender["id"])

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_foreign_owner"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {agent['name']} is not among "
        f"them because it belongs to a different account, and App MCP routes "
        f"over the caller's own agents. Share its bundle with this user and "
        f"have them install it — the session runs on the caller's own "
        f"install, so the install they own is the only thing this surface "
        f"can reach. (Reaching somebody else's agent is what identity "
        f"contacts are for, and that is a different question from this "
        f"one.)"
    )
    assert diagnosis["expected_agent_owner_email"] == owner["email"]


def test_app_mcp_verdict_when_the_agent_was_given_a_trigger_prompt_after_the_decision(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The App MCP half of `expected_agent_looks_reachable` — fully origin-
    neutral wording now (`_verdict_from_configuration`'s last branch never
    mentions `{surface}` at all), so this is byte-identical to the channel
    twin (`test_verdict_when_the_agent_was_given_a_trigger_prompt_after_the_
    decision`) apart from how the trace was produced. Replaces the old
    route-added-after-the-decision test: setting a trigger prompt is now the
    only way to move an agent into this branch on either surface.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "FixedSince")
    trace = _seed_app_mcp_trace(user["id"])
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


# ---------------------------------------------------------------------------
# Remedy profiles — the two arms the surfaces above cannot reach
#
# `RoutingReachabilityService` picks its remedy wording from a profile mapped
# off `trace.origin` (`_ORIGIN_PROFILES`). Two of those arms have no live
# producer that can be driven from here, and both are the kind that fails
# quietly:
#
# - **generic**, for an origin the map does not know. It replaced a default
#   that handed every unmapped origin the *App MCP* wording, on the argument
#   that App MCP's is the narrower of two. Narrow is not neutral, so the
#   sentence below must name no surface at all.
# - **email**, which is mapped to the channel profile. That mapping is not a
#   wording nicety: the channel arm is the one that consults channel policy,
#   and an email trace outside it loses `no_candidates_channel_scope` /
#   `no_candidates_auto_install_off` outright. The end-to-end proof over the
#   real polled path is
#   `tests/api/server_channels/server_channels_email_test.py`'s
#   `test_an_email_verdict_still_speaks_in_channel_terms`; what is pinned here
#   is the mapping itself, on a branch that says the surface's name out loud.
#
# Seeded rather than simulated, for the reason the App MCP section gives: these
# are branches whose answer comes from the database, and `POST
# /admin/routing/simulate` only ever produces a channel decision — it cannot
# forge an origin.
# ---------------------------------------------------------------------------


def _seed_trace(user_id: str, origin: str) -> dict:
    """A stored decision on `origin`, with no candidate list. See above."""
    trace_id = seed_routing_trace(
        created_at=datetime.now(UTC),
        origin=origin,
        user_id=user_id,
        outcome="no_match",
        message="please do the thing",
    )
    assert trace_id is not None, "seeded trace was not persisted"
    return {"id": str(trace_id)}


def test_an_unknown_origin_gets_the_generic_remedy_rather_than_app_mcps(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An origin no build has heard of is described, not mis-described.

    The foreign-owner branch is the one place a verdict still names the surface
    out loud, so it is where an unknown origin's wording is visible at all. It
    used to read "App MCP routes over the caller's own agents" — a claim about
    a surface this decision demonstrably did not run on, and the §2.4 defect in
    its purest form: confidently wrong beats coarse nowhere in this module.

    Two assertions, and the second is the one that would catch a regression to
    the old default: the sentence names no surface by name.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")
    trace = _seed_trace(sender["id"], "a_surface_from_the_future")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_foreign_owner"
    assert diagnosis["verdict"] == (
        f"This user has 0 eligible candidates; {agent['name']} is not among "
        f"them because it belongs to a different account, and this surface "
        f"routes over the caller's own agents. Share its bundle with this user "
        f"and have them install it — the session runs on the caller's own "
        f"install, so the install they own is the only thing this surface can "
        f"reach. (Reaching somebody else's agent is what identity contacts are "
        f"for, and that is a different question from this one.)"
    )
    assert "App MCP" not in diagnosis["verdict"]


def test_an_unknown_origin_still_gets_a_verdict_rather_than_an_error(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Degrading, not raising, is the whole contract of the generic arm.

    `diagnose` is total — a failure comes back as `unavailable` and the trace
    detail survives — so an origin lookup that raised would not show up as a
    500 here. It would show up as every verdict on that origin collapsing into
    "could not be computed", which reads like nothing happened. So this pins
    the real code, not merely the absence of an exception.
    """
    user, _ = _user(client, superuser_token_headers)
    trace = _seed_trace(user["id"], "a_surface_from_the_future")

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider, so no message from them can route "
        "anywhere. Give this user an agent with a router trigger prompt (or "
        "example prompts) on its Configuration tab."
    )


def test_an_email_trace_is_diagnosed_as_a_channel_not_as_app_mcp(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`origin="email"` maps to the channel profile, and says so out loud.

    Same branch as the unknown-origin test above, for the discrimination it
    gives: the surface noun is "a channel" and not "App MCP" or the generic
    "this surface". An email channel *is* a `ServerChannel` — policy, Pass 2,
    the pin — and phase 6 changed only its label.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, _ = _user(client, superuser_token_headers)
    agent = _agent(client, owner_headers, "SomebodyElses")
    trace = _seed_trace(sender["id"], "email")

    diagnosis = _diagnosis(
        client, superuser_token_headers, trace, expected_agent_id=agent["id"]
    )
    assert diagnosis["code"] == "expected_agent_foreign_owner"
    assert "and a channel routes over the caller's own agents" in (
        diagnosis["verdict"]
    ), diagnosis
    assert "App MCP" not in diagnosis["verdict"]
    assert "this surface routes over" not in diagnosis["verdict"]


def test_an_email_verdict_keeps_the_channel_arms_auto_install_remedy(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The half of the email mapping that is not wording at all.

    Only a profile that reads channel policy calls `_channel_pass_2_block`, and
    only that call can return `no_candidates_channel_scope` /
    `no_candidates_auto_install_off`. An email trace mapped to any other
    profile would lose those two codes silently — no test would go red, and the
    reader would simply never be told that the channel's own settings stopped
    Pass 2. What this pins is the cheapest visible consequence of being in that
    arm at all: the auto-install clause, which the non-channel `no_candidates`
    sentence does not contain.

    The seeded row names no channel, so `_channel_pass_2_block` finds no policy
    to blame and the branch falls through to the channel arm's own base
    sentence — which is exactly the discrimination wanted here. The end-to-end
    version over a real polled email is
    `tests/api/server_channels/server_channels_email_test.py`'s
    `test_an_email_verdict_still_speaks_in_channel_terms`.
    """
    user, _ = _user(client, superuser_token_headers)
    trace = _seed_trace(user["id"], "email")

    diagnosis = _diagnosis(client, superuser_token_headers, trace)
    assert diagnosis["code"] == "no_candidates"
    assert diagnosis["verdict"] == (
        "This user had no routing candidates at all: they own no agent the "
        "classifier could consider and no auto-install bundle was eligible, so "
        "no message from them can route anywhere. Set a router trigger prompt "
        "(or example prompts) on the agent you expected, from its "
        "Configuration tab, or add its bundle to the auto-install list."
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
