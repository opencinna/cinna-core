"""Pass 1's conditional `only_one` short-circuit, and the trace it still writes.

A ballot with exactly one eligible candidate routes to it without asking a
model — **provided Pass 2 has nothing to offer this sender**. The condition is
the whole feature, and the reasoning is worth restating here because the two
unconditional forms are both wrong and both look reasonable:

*Never short-circuit* pays a provider cascade on every inbound message, on an
externally triggerable path, to answer a question with one possible answer.

*Always short-circuit* is worse, and the failure is not an edge case. A newly
auto-registered sender owns **zero** agents; the moment Pass 2 onboards them
they own **exactly one**. "Exactly one eligible agent" is therefore the
immediate post-onboarding state of every user auto-install has ever served, so
an unconditional short-circuit would make the onboarding message the last one
that could ever reach the catalog. The governing principle:

    A short-circuit is sound only when there is no alternative to choose
    between — and Pass 2's candidates are part of the choice space.

Note what the short-circuit is *not* a risk to, because the objection recorded
in the code before this landed said otherwise. The hazard in the old `only_one`
this pass ran before the scope split was never the short-circuit; it was the set
it ran over, where the lone candidate could be a foreign agent or an identity
contact route. `ChannelCandidateProvider` eliminated that class, so over the
corrected set the one candidate is the sender's own agent by construction.

**Naming no classifier answer is an assertion here, not an omission.** The
helpers' default stub raises (`tests.utils.routing.refuse_to_classify`), so a
test that names none fails loudly if the classifier is reached — which is
exactly how "it short-circuited" is proved, rather than by reading a
`match_method` that a lucky stub could also produce.

Driven through `POST /admin/routing/simulate` (the real router, real state, no
side effects) except where the branch is about the real webhook path.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
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
from tests.utils.routing import (
    classification,
    get_routing_trace,
    patched_routing_externals,
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


def _agent(
    client: TestClient,
    headers: dict[str, str],
    label: str,
    *,
    trigger_prompt: str | None = None,
) -> dict:
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    if trigger_prompt is not None:
        set_router_trigger_prompt(client, headers, agent["id"], trigger_prompt)
    return agent


def _auto_install_bundle(
    client: TestClient,
    superuser_headers: dict[str, str],
    label: str,
    *,
    trigger_prompt: str | None = "Handle catalog requests",
) -> dict:
    """A published public bundle, on the server-wide auto-install list.

    ``trigger_prompt=None`` publishes a revision with none, which Pass 2 records
    as a ``no_trigger_prompt`` skip rather than offering it — the shape used
    below to prove skips are written exactly once.
    """
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_headers, publisher["id"])
    source = _agent(client, publisher_headers, label, trigger_prompt=trigger_prompt)
    publish_bundle_and_make_public(client, publisher_headers, source["id"])
    published = client.get(
        f"{API}/agents/{source['id']}", headers=publisher_headers
    ).json()
    add_auto_install_bundle(
        client, superuser_headers, str(published["bundle_uuid"])
    )
    return published


def _trace(
    client: TestClient,
    superuser_headers: dict[str, str],
    user_id: str,
    *,
    message: str = "please handle this",
    include_catalog: bool = True,
    **classifier,
) -> dict:
    with patched_routing_externals(**classifier):
        simulated = simulate_routing(
            client,
            superuser_headers,
            message=message,
            as_user_id=user_id,
            include_catalog=include_catalog,
        )
    return get_routing_trace(client, superuser_headers, simulated["id"])


def _stage(trace: dict, name: str) -> dict | None:
    return next((s for s in trace["stages"] if s["stage"] == name), None)


def _rows(trace: dict) -> list[dict]:
    return [c for stage in trace["stages"] for c in stage["candidates"]]


# ---------------------------------------------------------------------------
# The four branches of the behaviour table
# ---------------------------------------------------------------------------


def test_one_eligible_candidate_and_an_empty_catalog_routes_without_a_model(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Branch 1. Nothing to choose between, so nothing to ask a model about."""
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Only", trigger_prompt="Handle anything")

    trace = _trace(client, superuser_token_headers, user["id"])

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "only_one"
    assert trace["selected_agent_id"] == agent["id"]


def test_one_eligible_candidate_among_several_owned_agents_still_short_circuits(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Branch 1, at the boundary that is easiest to implement wrongly.

    The condition is **one eligible candidate**, counted after the provider's
    filter — not one owned agent. This sender owns three; two have neither a
    trigger prompt nor example prompts, so the ballot handed to the classifier
    would have held exactly one entry, and there is still nothing to choose
    between.

    The two ineligible ones are not merely absent: they are on the trace as
    `no_trigger_prompt` skips, because a fast path that stopped explaining
    itself would take the diagnosis away precisely when routing was easy.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Eligible", trigger_prompt="Handle anything")
    _agent(client, headers, "Wordless1")
    _agent(client, headers, "Wordless2")

    trace = _trace(client, superuser_token_headers, user["id"])

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "only_one"
    assert trace["selected_agent_id"] == agent["id"]

    rows = _rows(trace)
    assert sorted(r["skip_reason"] or "" for r in rows) == [
        "",
        "no_trigger_prompt",
        "no_trigger_prompt",
    ], rows
    assert [r["ref_id"] for r in rows if r["eligible"]] == [agent["id"]]


def test_one_eligible_candidate_with_a_bundle_on_offer_asks_the_classifier(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Branch 2 — the one that keeps auto-install reachable after onboarding.

    The sender owns exactly one eligible agent, but the auto-install list holds
    a bundle they could still be given, so the choice space is bigger than one
    and *"none of mine"* has to stay a reachable answer. Under an unconditional
    short-circuit this message could never reach the catalog again.
    """
    _auto_install_bundle(client, superuser_token_headers, "OnOffer")
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Owned", trigger_prompt="Handle anything")

    trace = _trace(
        client,
        superuser_token_headers,
        user["id"],
        classify_result=classification(agent["id"]),
    )

    assert trace["outcome"] == "routed", trace
    # `ai`, not `only_one`: the classifier ran and picked, which is the whole
    # point of the branch.
    assert trace["match_method"] == "ai"
    assert trace["selected_agent_id"] == agent["id"]


def test_two_eligible_candidates_always_classify(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Branch 3. Unchanged, and never probed.

    With two candidates the choice space is already bigger than one, so what
    Pass 2 holds cannot change whether the classifier runs — which is why the
    availability probe is confined to the single-candidate branch rather than
    run on every inbound message.
    """
    user, headers = _user(client, superuser_token_headers)
    first = _agent(client, headers, "First", trigger_prompt="eigenvalue questions")
    _agent(client, headers, "Second", trigger_prompt="calendar bookings")

    trace = _trace(
        client,
        superuser_token_headers,
        user["id"],
        classify_result=classification(first["id"]),
    )

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "ai"
    assert trace["selected_agent_id"] == first["id"]


def test_zero_eligible_candidates_still_falls_through_to_pass_2(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Branch 4. Unchanged, and the case Pass 2 was built for.

    The short-circuit must not swallow the empty ballot: a sender who owns
    nothing the classifier can consider is exactly who auto-install onboards.
    """
    bundle = _auto_install_bundle(client, superuser_token_headers, "Onboard")
    user, headers = _user(client, superuser_token_headers)
    _agent(client, headers, "Wordless")  # owned, but not a candidate

    trace = _trace(
        client,
        superuser_token_headers,
        user["id"],
        classify_result=classification(bundle["bundle_uuid"]),
    )

    assert trace["outcome"] == "parked_install", trace
    assert trace["selected_bundle_uuid"] == str(bundle["bundle_uuid"])
    assert _stage(trace, "pass_2") is not None, trace["stages"]


# ---------------------------------------------------------------------------
# include_catalog
# ---------------------------------------------------------------------------


def test_include_catalog_off_short_circuits_even_with_a_bundle_on_offer(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """With the catalog switched off Pass 2 cannot run, so the choice space *is*
    one and the short-circuit applies.

    Setup is deliberately the branch-2 setup — a bundle the sender could be
    offered is on the auto-install list — with the single flag flipped. Simulate
    exposes `include_catalog` so an admin can ask "would this have matched
    something they already have?", and an answer that diverged from the real
    path's would make that question useless.

    No classifier answer is named: reaching it would mean Pass 1 consulted a
    catalog that could not have run.
    """
    _auto_install_bundle(client, superuser_token_headers, "Unreachable")
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Owned", trigger_prompt="Handle anything")

    trace = _trace(
        client, superuser_token_headers, user["id"], include_catalog=False
    )

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "only_one"
    assert trace["selected_agent_id"] == agent["id"]
    # Pass 2 was never looked at, so there is no scan to report.
    assert _stage(trace, "pass_2") is None, trace["stages"]


# ---------------------------------------------------------------------------
# Once-only skip recording — on both paths
# ---------------------------------------------------------------------------


def test_the_availability_scan_is_recorded_once_when_pass_1_short_circuits(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The probe found the catalog empty *for this sender* — and says why.

    The bundle is on the auto-install list and this sender already installed
    it, so Pass 2 has nothing to offer and Pass 1 short-circuits. Pass 2 then
    never runs, which makes this the scan's only chance to be recorded: without
    it, "why was this user offered nothing?" would have no answer at all in the
    trace, going quiet exactly when an admin is asking.

    The stage is marked as an availability check. A `pass_2` heading carrying
    candidate rows and no verdict is otherwise indistinguishable from a Pass 2
    that ran and found nothing, which is a different finding.
    """
    bundle = _auto_install_bundle(client, superuser_token_headers, "AlreadyMine")
    user, headers = _user(client, superuser_token_headers)
    installed = install_bundle(client, headers, bundle["bundle_id"])

    trace = _trace(client, superuser_token_headers, user["id"])

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "only_one"
    assert trace["selected_agent_id"] == installed["id"]

    pass_2 = _stage(trace, "pass_2")
    assert pass_2 is not None, trace["stages"]
    assert [c["ref_id"] for c in pass_2["candidates"]] == [str(bundle["bundle_uuid"])]
    assert pass_2["candidates"][0]["skip_reason"] == "already_installed"
    assert pass_2["match_method"] is None, "nothing classified on this stage"
    assert pass_2["reason"] is not None and "availability only" in pass_2["reason"]


def test_the_availability_scan_is_recorded_once_when_pass_2_then_runs(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The probe ran, the classifier missed, and Pass 2 reused the scan.

    The failure this pins is duplication: the scan happens in Pass 1's session
    and Pass 2 would otherwise redo it, writing every bundle to the trace a
    second time and doubling the candidate counts the admin list reports. Each
    row appears exactly once because the gather returns plain data and only one
    caller commits it.

    Two bundles, so both halves of the ballot are covered — one offered, one
    skipped for carrying no trigger prompt.
    """
    offered = _auto_install_bundle(client, superuser_token_headers, "Offered")
    skipped = _auto_install_bundle(
        client, superuser_token_headers, "NoPrompt", trigger_prompt=None
    )
    user, headers = _user(client, superuser_token_headers)
    _agent(client, headers, "Owned", trigger_prompt="Handle anything")

    # One stub answers both passes. Naming the *bundle* makes Pass 1 miss (the
    # id is not on its ballot) and Pass 2 hit, which is the reuse path.
    trace = _trace(
        client,
        superuser_token_headers,
        user["id"],
        classify_result=classification(offered["bundle_uuid"]),
    )

    assert trace["outcome"] == "parked_install", trace
    assert [s["stage"] for s in trace["stages"]] == ["pass_1", "pass_2"], trace["stages"]

    refs = [c["ref_id"] for c in _rows(trace)]
    assert refs.count(str(offered["bundle_uuid"])) == 1, refs
    assert refs.count(str(skipped["bundle_uuid"])) == 1, refs

    pass_2 = _stage(trace, "pass_2")
    assert pass_2["match_method"] == "ai"
    # A Pass 2 that really classified is not labelled as an availability check.
    assert "availability only" not in (pass_2["reason"] or "")
    # ...and its surviving candidate IS eligible, because a classifier did get
    # it. Contrast the availability-only test below.
    offered_row = next(
        c for c in pass_2["candidates"] if c["ref_id"] == str(offered["bundle_uuid"])
    )
    assert offered_row["eligible"] is True, offered_row


def test_a_probe_that_pass_1_beat_does_not_inflate_the_eligible_count(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The availability scan's survivors are recorded as skipped, not eligible.

    `eligible=True` is not a description of a bundle — it is an input to
    arithmetic the diagnostic surface performs. A bundle that was available but
    never put to a classifier, marked eligible, would join the "chosen from N
    eligible candidates" the verdict prints, the list view's `candidate_count`,
    and the near-miss ranking; worst of all, asking the reachability verdict
    about it would produce *"X was an eligible candidate and the classifier did
    not pick it"* — about a classifier that was never given X.

    So this pins the number, not just the flag: one owned agent won, and the
    decision reports exactly **one** eligible candidate however many bundles the
    probe happened to look at.
    """
    offered = _auto_install_bundle(client, superuser_token_headers, "Beaten")
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "Owned", trigger_prompt="Handle anything")

    trace = _trace(
        client,
        superuser_token_headers,
        user["id"],
        classify_result=classification(agent["id"]),
    )

    assert trace["outcome"] == "routed", trace
    assert trace["selected_agent_id"] == agent["id"]

    eligible = [r for r in _rows(trace) if r["eligible"]]
    assert [r["ref_id"] for r in eligible] == [agent["id"]], eligible

    beaten = next(
        r for r in _rows(trace) if r["ref_id"] == str(offered["bundle_uuid"])
    )
    assert beaten["eligible"] is False
    assert beaten["skip_reason"] == "pass_1_matched"

    diagnosis = get_routing_trace(
        client, superuser_token_headers, trace["id"]
    )["diagnosis"]
    assert diagnosis["eligible_candidate_count"] == 1, diagnosis
    assert "chosen from 1 eligible candidate." in diagnosis["verdict"], diagnosis

    # And the verdict about the beaten bundle says the true thing rather than
    # blaming a classifier that never saw it.
    about_bundle = get_routing_trace(
        client,
        superuser_token_headers,
        trace["id"],
        expected_agent_id=str(offered["bundle_uuid"]),
    )["diagnosis"]
    assert about_bundle["code"] == "expected_agent_skipped"
    assert "Pass 1 matched one of this sender's own agents first" in (
        about_bundle["verdict"]
    ), about_bundle


def test_an_owned_agent_with_only_example_prompts_is_the_single_candidate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Eligibility is "anything to classify on", and examples are text too.

    Worth pinning against the short-circuit specifically: the branch counts
    *eligible* candidates, so an eligibility rule that overlooked
    `example_prompts` would not merely fail to route — it would report zero
    candidates and hand the sender to the auto-install catalog instead.
    """
    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "ExamplesOnly")
    update_agent(
        client, headers, agent["id"], example_prompts=["restart the payment worker"]
    )

    trace = _trace(client, superuser_token_headers, user["id"])

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "only_one"
    assert trace["selected_agent_id"] == agent["id"]


def test_a_failed_catalog_scan_classifies_rather_than_short_circuiting(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An availability probe must never *decide* anything.

    A scan that failed leaves the choice space **unknown**, and unknown must not
    be read as empty: the failure mode being bought off is a catalog outage
    quietly changing which agent a message reaches, with a routed trace and no
    sign anything went wrong. Degrading to "ask the classifier" costs an LLM
    call and changes no answer.

    The scan is failed at `_gather_catalog_candidates`, which is the shared
    gather both the probe and Pass 2 use, so this also covers Pass 2 recomputing
    from scratch rather than trusting the unusable ballot.
    """
    from unittest.mock import patch

    user, headers = _user(client, superuser_token_headers)
    agent = _agent(client, headers, "SoleAgent", trigger_prompt="Handle anything")

    with patch(
        "app.services.server_channels.channel_routing_service."
        "ChannelRoutingService._gather_catalog_candidates",
        side_effect=RuntimeError("catalog unavailable"),
    ):
        trace = _trace(
            client,
            superuser_token_headers,
            user["id"],
            classify_result=classification(agent["id"]),
        )

    assert trace["outcome"] == "routed", trace
    assert trace["match_method"] == "ai", "an unknown choice space must not short-circuit"
    assert trace["selected_agent_id"] == agent["id"]
