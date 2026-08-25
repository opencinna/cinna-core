"""What an identity decision writes into the routing trace — and what it must not.

Three groups, all driven through a **real webhook delivery**, which is what
separates this file from the two `xfail`ed tests it replaces
(`tests/api/routing/routing_identity_stage2_capture_test.py`, deleted in the
same change).

Those two drove `POST /admin/routing/simulate`, and could never pass as they
stood: a simulate that names no channel resolves
`ResolvedChannelPolicy.for_no_channel()`, whose `allow_identity_routing` is
`False` by design, so identity cannot enter that ballot at all, whatever the
setup on the target user. `RoutingSimulateRequest` has since gained a
`channel_id` (phase 6 of the channels & identity unification), so a simulate
*can* now decide under a real channel's policy — but that does not make it the
vehicle for these facts either. What is pinned below is what a real inbound
identity decision writes into the trace, which needs the trace to belong to a
real channel **and** to have been produced by the real inbound path; simulate
satisfies only the first. The second of the pair also asserted
`match_method == "pattern"`, a mechanism deleted by settled decision §2.9 —
`IdentityAgentBinding.message_patterns` is no longer read by anything. Both
facts they were reaching for are re-asserted below, at equal or greater
strength, on the surface that can actually produce them.

  1. **The deliberate silence.** With `allow_identity_routing` off, the
     identity provider is not called at all, so the identity owners this sender
     could have reached leave **no rows** — not even skips. That inverts master
     plan §3.5 ("every provider records skips"), on purpose and in exactly one
     place: recording them would publish the existence of other people's
     identities into a trace an external sender can trigger at will, one row
     per person who has ever named them. Asserted positively — no
     `SKIP_IDENTITY_UNAVAILABLE`, no `identity:`-prefixed `ref_id`, no
     `source="identity"` — and paired with the same setup switched on, so the
     absence cannot be passing for a trivial reason.
  2. **The Stage-2 ballot.** Every accessible binding is recorded with the
     trigger prompt and `prompt_examples` the classifier was handed, because
     both come from the one builder
     (`IdentityRoutingService._binding_candidates`). And `match_method` is in
     the current vocabulary: `only_one` on the single-binding shortcut, and
     never `pattern` on any branch.
  3. **`SKIP_IDENTITY_UNAVAILABLE` is genuinely producible on a channel now** —
     an identity owner named this sender, but nothing they shared is switched
     on — which is why its reachability verdict is pinned here rather than in
     `tests/unit/test_routing_reachability.py` alongside the codes no live
     surface can produce. It is still **not** producible via simulate, for the
     `for_no_channel()` reason above.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import share_identity_agent
from tests.utils.routing import (
    classification,
    get_routing_trace,
    list_routing_traces,
    post_channel_message,
)
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import update_my_channel
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_IDENTITY_REF_PREFIX = "identity:"
_SKIP_IDENTITY_UNAVAILABLE = "identity_unavailable"


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _agent_owner(client, superuser_headers) -> tuple[dict, dict[str, str]]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _agent(client, headers, label: str) -> dict:
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    return agent


def _trace_ids(client, superuser_headers, channel) -> set[str]:
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    return {row["id"] for row in page["data"]}


def _deliver(client, superuser_headers, channel, signer, sender_email, text, **kwargs):
    """One webhook delivery; returns ``(trace, send_mock)`` for the new row.

    Trace ids are snapshotted before and diffed after rather than indexed into:
    the admin list orders by ``created_at DESC, id DESC``, and two deliveries
    inside one test land close enough in time that a random UUID is what
    decides the order (see ``tests/api/routing/README.md``).
    """
    before = _trace_ids(client, superuser_headers, channel)
    event = build_message_event(
        thread_key=f"spaces/AAA/threads/{random_lower_string()}",
        text=text,
        sender_email=sender_email,
    )
    resp, send_mock = post_channel_message(client, channel, signer, event, **kwargs)
    assert resp.status_code == 200
    new = _trace_ids(client, superuser_headers, channel) - before
    assert len(new) == 1, new
    trace = get_routing_trace(client, superuser_headers, new.pop())
    return trace, send_mock


def _stage(trace: dict, name: str) -> dict | None:
    return next((s for s in trace["stages"] if s["stage"] == name), None)


def _all_candidates(trace: dict) -> list[dict]:
    return [c for stage in trace["stages"] for c in stage["candidates"]]


# ---------------------------------------------------------------------------
# 1. The deliberate silence (plan §2.1 — the one inversion of master plan §3.5)
# ---------------------------------------------------------------------------


def test_identity_is_absent_from_the_trace_entirely_when_the_switch_is_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """With the switch off, an unreachable identity leaves nothing behind.

    The setup is a *reachable* identity in every respect except the sender's
    own channel-level consent: HR shared an agent, the sender enabled the
    contact, and the binding is live. Only `allow_identity_routing` is off. So
    the absence below is caused by that switch and nothing else — which is what
    the second half proves, by flipping it on and watching the same identity
    appear.

    Asserted positively, three ways, because this is the one place the "never
    drop a candidate silently" rule is deliberately inverted and every one of
    those three is easy for a later "fix" to defeat:

      - no candidate row carries `skip_reason="identity_unavailable"`;
      - no candidate row carries an `identity:`-prefixed `ref_id`;
      - no candidate row carries `source="identity"`, and no `identity_stage2`
        stage exists.

    The *reason* it must stay this way is not aesthetic: with the switch off,
    an external sender could otherwise trigger, at will, a trace enumerating
    every colleague who has ever named them on an identity binding.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _agent_owner(client, superuser_token_headers)
    hr_agent = _agent(client, owner_headers, "HRSilent")
    sender, sender_headers = create_random_user_with_headers(client)
    share_identity_agent(
        client,
        owner_headers,
        sender_headers,
        agent_id=hr_agent["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Answer time-off questions",
    )

    # ── Phase 1: switch off (the default — no settings row at all) ─────────
    trace, send_mock = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "hey, ask HR what is my time-off status?",
    )
    assert any(
        "couldn't find an assistant" in (c.args[-1] or "")
        for c in send_mock.await_args_list
    ), [c.args[-1] for c in send_mock.await_args_list]

    candidates = _all_candidates(trace)
    assert [
        c for c in candidates if c["skip_reason"] == _SKIP_IDENTITY_UNAVAILABLE
    ] == [], candidates
    assert [
        c for c in candidates
        if str(c["ref_id"] or "").startswith(_IDENTITY_REF_PREFIX)
    ] == [], candidates
    assert [c for c in candidates if c["source"] == "identity"] == [], candidates
    assert _stage(trace, "identity_stage2") is None, trace["stages"]
    # The owner's own identifying details are nowhere in the row either.
    assert owner["email"] not in (trace.get("message_text") or "")
    assert [c for c in candidates if c.get("owner_email") == owner["email"]] == []

    # ── Phase 2: the same setup with the switch on — the identity appears ──
    # Without this half, Phase 1 would pass against an implementation where
    # identity routing simply does not work.
    row = update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )
    assert row["allow_identity_routing"] is True, row

    trace_on, _ = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "hey, ask HR what is my time-off status?",
    )
    identity_rows = [
        c for c in _all_candidates(trace_on)
        if str(c["ref_id"] or "").startswith(_IDENTITY_REF_PREFIX)
    ]
    assert len(identity_rows) == 1, _all_candidates(trace_on)
    assert identity_rows[0]["ref_id"] == f"{_IDENTITY_REF_PREFIX}{owner['id']}"
    assert identity_rows[0]["source"] == "identity"
    assert identity_rows[0]["eligible"] is True, identity_rows[0]
    assert _stage(trace_on, "identity_stage2") is not None, trace_on["stages"]


# ---------------------------------------------------------------------------
# 2. The Stage-2 ballot (replaces the first deleted xfail)
# ---------------------------------------------------------------------------


def test_identity_stage2_records_its_candidate_ballot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Every accessible binding is on the recorded ballot, with its wording.

    Two bindings rather than one, deliberately: a single accessible binding
    takes Stage 2's own `only_one` shortcut, which would prove the capture
    happens on the *easy* branch only. Two forces the classifier branch — the
    one whose ballot used to be invisible.

    Stage 1 still does not classify (the sender owns nothing and can reach one
    identity owner, so Pass 1 short-circuits), which makes the single named
    classifier answer below unambiguously Stage 2's.

    `trigger_prompt` and `prompt_examples` are asserted on the recorded rows
    because both now come from `IdentityRoutingService._binding_candidates` —
    the single builder that feeds the classifier *and* the capture, so the
    tuning card can never show a ballot the classifier did not receive.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _agent_owner(client, superuser_token_headers)
    sender, sender_headers = create_random_user_with_headers(client)

    calendar = _agent(client, owner_headers, "IdentityCalendar")
    expenses = _agent(client, owner_headers, "IdentityExpenses")
    share_identity_agent(
        client, owner_headers, sender_headers,
        agent_id=calendar["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Handle calendar questions",
        prompt_examples="when am I free\nbook me a slot",
    )
    share_identity_agent(
        client, owner_headers, sender_headers,
        agent_id=expenses["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Handle expense questions",
    )
    update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )

    trace, _ = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "ask them when am I free on friday",
        classify_result=classification(calendar["id"]),
    )

    stage2 = _stage(trace, "identity_stage2")
    assert stage2 is not None, trace["stages"]

    names = {c["name"] for c in stage2["candidates"]}
    assert calendar["name"] in names, stage2["candidates"]
    assert expenses["name"] in names, stage2["candidates"]

    row = next(c for c in stage2["candidates"] if c["name"] == calendar["name"])
    assert row["ref_id"] == calendar["id"], row
    assert row["trigger_prompt"] == "Handle calendar questions", row
    assert row["prompt_examples"] == "when am I free\nbook me a slot", row
    assert row["source"] == "identity", row
    assert row["eligible"] is True, row

    other = next(c for c in stage2["candidates"] if c["name"] == expenses["name"])
    assert other["trigger_prompt"] == "Handle expense questions", other
    assert other["prompt_examples"] is None, other

    # The decision went where the classifier said, in the identity owner's
    # workspace — the ballot is not a decoration recorded beside a different
    # answer.
    assert trace["outcome"] == "routed", trace
    assert trace["selected_agent_id"] == calendar["id"], trace
    owner_sessions = [
        s for s in list_sessions(client, owner_headers)
        if s["agent_id"] == calendar["id"]
    ]
    assert len(owner_sessions) == 1, owner_sessions

    # In the current vocabulary, and never the deleted one.
    assert stage2["match_method"] in (None, "only_one", "ai"), stage2
    assert stage2["match_method"] != "pattern", stage2
    assert stage2["matched_pattern"] is None, stage2


def test_identity_stage2_match_method_is_only_one_on_the_single_binding_shortcut(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The positive half of the vocabulary claim.

    One accessible binding: Stage 2 takes its own shortcut and records
    `match_method="only_one"` under the `identity_stage2` stage — a value in
    the current vocabulary, on the branch that actually sets one. Asserting
    only "not pattern" would be satisfied by a stage that recorded nothing at
    all, which is the *lying field* the deleted test was written against in the
    first place.

    Note the decision-level `match_method` is `only_one` too, from Pass 1's own
    short-circuit — the trace reports how the **last** stage matched, and both
    stages matched that way here.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _agent_owner(client, superuser_token_headers)
    sender, sender_headers = create_random_user_with_headers(client)
    only = _agent(client, owner_headers, "IdentityOnly")
    share_identity_agent(
        client, owner_headers, sender_headers,
        agent_id=only["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Handle everything",
    )
    update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )

    # No classifier answer named: neither stage may classify.
    trace, _ = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "ask them about anything",
    )

    stage2 = _stage(trace, "identity_stage2")
    assert stage2 is not None, trace["stages"]
    assert stage2["match_method"] == "only_one", stage2
    assert stage2["matched_pattern"] is None, stage2
    assert trace["outcome"] == "routed", trace
    assert trace["selected_agent_id"] == only["id"], trace


def test_a_binding_glob_no_longer_wins_and_never_reports_a_pattern_match(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Settled decision §2.9, executed rather than assumed.

    `IdentityAgentBinding.message_patterns` still exists as a column (a later
    phase drops it) and the create route still accepts it, so "nobody reads it
    any more" is exactly the kind of claim that rots silently. Here a binding
    carries a glob that the message hits verbatim, and the classifier is told
    to pick the **other** agent. The other agent must win, and no stage may
    report `match_method="pattern"` or a `matched_pattern`.

    This replaces the deleted `..._pattern_hit_reports_its_match_method` test.
    That one asserted the pattern branch recorded its match honestly; the
    branch is gone, so the equivalent-or-stronger statement is that the glob
    has no effect at all and leaves no trace of having had one.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _agent_owner(client, superuser_token_headers)
    sender, sender_headers = create_random_user_with_headers(client)

    patterned = _agent(client, owner_headers, "IdentityPatterned")
    other = _agent(client, owner_headers, "IdentityOther")
    share_identity_agent(
        client, owner_headers, sender_headers,
        agent_id=patterned["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Handle signature requests",
        message_patterns="sign this document *",
    )
    share_identity_agent(
        client, owner_headers, sender_headers,
        agent_id=other["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Handle everything else",
    )
    update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )

    trace, _ = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "sign this document please",
        classify_result=classification(other["id"]),
    )

    # The glob matched the message and lost anyway.
    assert trace["selected_agent_id"] == other["id"], trace
    assert trace["outcome"] == "routed", trace
    for stage in trace["stages"]:
        assert stage["match_method"] != "pattern", stage
        assert stage["matched_pattern"] is None, stage
    owner_sessions = [
        s for s in list_sessions(client, owner_headers)
        if s["agent_id"] == other["id"]
    ]
    assert len(owner_sessions) == 1, owner_sessions
    assert [
        s for s in list_sessions(client, owner_headers)
        if s["agent_id"] == patterned["id"]
    ] == []


# ---------------------------------------------------------------------------
# 3. SKIP_IDENTITY_UNAVAILABLE — reachability verdict, on the webhook path
# ---------------------------------------------------------------------------


def test_verdict_when_an_identity_owner_shared_nothing_the_sender_can_reach(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The one skip reason this phase made producible on a channel.

    An identity owner named this sender on a binding, so the provider records
    them — but the sender never enabled the contact, so nothing they shared is
    reachable and they never make the ballot. That is
    `SKIP_IDENTITY_UNAVAILABLE`, and it is now a **live** channel row rather
    than an App-MCP-only one, which is why this test lives on the webhook
    surface and not with the unproducible codes in
    `tests/unit/test_routing_reachability.py`.

    It is still not producible via `POST /admin/routing/simulate` — see the
    module docstring — so the branch has exactly one live producer, and it is
    the one driven here.

    Asserted in three layers, narrow to broad:
      1. the recorded candidate row itself (ref, source, reason, and the
         classifier-facing wording the provider composes for a person);
      2. the diagnosis's `skipped_by_reason` tally;
      3. the verdict sentence, pinned verbatim — the wording *is* the feature,
         and a test that pinned only `code` would keep passing while the
         sentence drifted into saying something false.

    One thing deliberately NOT asserted here, because it cannot be reached:
    the channel-voiced *skip explanation* for this reason
    (`_CHANNEL_SKIP_EXPLANATIONS[SKIP_IDENTITY_UNAVAILABLE]`) is only rendered
    on the `?expected_agent_id=` branch, and that query parameter is typed
    `uuid.UUID` while an identity candidate's `ref_id` is the namespaced
    `identity:{owner_id}`. No UUID can name this row, so the override is
    unreachable through the API as it stands.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = _agent_owner(client, superuser_token_headers)
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
        # The sender never switched the contact on, so the assignment stays
        # `is_enabled=False` — the state the provider records as a skip.
        enable=False,
    )
    update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )

    # No classifier answer named: the ballot is empty (the only candidate was
    # skipped), and an empty ballot short-circuits before the classifier.
    trace, send_mock = _deliver(
        client,
        superuser_token_headers,
        channel,
        signer,
        sender["email"],
        "hey, ask HR what is my time-off status?",
    )

    # ── Layer 1: the recorded row ──────────────────────────────────────────
    candidates = _all_candidates(trace)
    skipped = [
        c for c in candidates if c["skip_reason"] == _SKIP_IDENTITY_UNAVAILABLE
    ]
    assert len(skipped) == 1, candidates
    row = skipped[0]
    assert row["ref_id"] == f"{_IDENTITY_REF_PREFIX}{owner['id']}", row
    assert row["source"] == "identity", row
    assert row["kind"] == "agent", row
    assert row["eligible"] is False, row
    assert row["owner_email"] == owner["email"], row
    # The classifier-facing wording for a *person*, composed by
    # `IdentityCandidateProvider._contact_trigger_prompt`. Pinned because an
    # edit to it is a routing-behaviour change, not a copy change.
    assert row["trigger_prompt"] == (
        f"Contact {owner['email']} ({owner['email']}). "
        f"Routes to their available agents."
    ), row
    # The agent behind the binding is NOT on the trace: the candidate is the
    # person, and Stage 2 never ran.
    assert [c for c in candidates if c["ref_id"] == hr_agent["id"]] == [], candidates
    assert _stage(trace, "identity_stage2") is None, trace["stages"]

    # ── Layer 2: the tally ─────────────────────────────────────────────────
    diagnosis = trace["diagnosis"]
    assert diagnosis is not None, trace
    assert diagnosis["skipped_by_reason"] == {_SKIP_IDENTITY_UNAVAILABLE: 1}, diagnosis
    assert diagnosis["eligible_candidate_count"] == 0, diagnosis

    # ── Layer 3: the sentence ──────────────────────────────────────────────
    assert diagnosis["code"] == "all_candidates_skipped", diagnosis
    assert diagnosis["verdict"] == (
        "This user has no eligible candidates: 1 candidate was excluded "
        f"before the classifier saw it ({_SKIP_IDENTITY_UNAVAILABLE}). "
        "Fix the exclusion on the agent you expected — the candidate table "
        "below names the reason for each one."
    ), diagnosis
    assert diagnosis["action"] in diagnosis["verdict"], diagnosis

    # And the sender is told nothing about any of it.
    assert any(
        "couldn't find an assistant" in (c.args[-1] or "")
        for c in send_mock.await_args_list
    ), [c.args[-1] for c in send_mock.await_args_list]
