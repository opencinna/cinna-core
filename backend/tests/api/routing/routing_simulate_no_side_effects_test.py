"""Simulate has no side effects — the safety property of Phase 3.

`POST /admin/routing/simulate` runs the real router over another user's real
routing state and then does *nothing* with the answer: no thread binding, no
session, no bundle install, no outbound reply. That is not enforced by a flag
threaded through the pipeline; `ChannelRoutingService.decide` cannot perform
any of those, and simulate calls only `decide`. These tests are what proves the
claim rather than restating it.

**Every absence here is asserted against durable state**, never against a log.
`caplog` is vacuous in this suite once Alembic's `fileConfig` has disabled the
app loggers, and the negative form (`assert x not in caplog.text`) then passes
forever against an empty string while reading, in review, exactly like a
careful absence proof. A no-side-effects assertion is the shape that invites
it. So:

  - **binding** — a direct `ChannelThreadBinding` count (documented exemption:
    bindings have no HTTP surface; `server_channels_pending_outbound_test.py`
    reads them the same way).
  - **session** — `GET /sessions/` as the target user.
  - **install** — `GET /agents/` as the target user.
  - **outbound reply** — the channel debug buffer read across *every* channel
    key (an outbound event is recorded for every real reply), plus the adapter
    `send_message` mock. Read across every key, not scoped to this test's
    channel: see `_outbound_events`, where the scoped version was found to be
    unfalsifiable.

**These assertions were mutation-checked, not merely written.** Four absences
is the easiest thing in this codebase to assert in a form that cannot fail. The
binding assertion was verified by making `decide` create a binding on purpose
and watching this file go red; the revert is recorded in the phase notes. If
you add an absence assertion here, break the thing it guards first.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import ChannelThreadBinding
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.mfa import find_security_events
from tests.utils.routing import (
    get_routing_trace,
    list_routing_traces,
    patched_routing_externals,
    simulate_routing,
)
from tests.utils.server_channel import add_auto_install_bundle, create_server_channel
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


# ---------------------------------------------------------------------------
# State snapshots — every one of these reads durable state, never a log
# ---------------------------------------------------------------------------


def _binding_count(db: Session) -> int:
    """Every channel thread binding in the database.

    EXEMPTION — `ChannelThreadBinding` has no HTTP surface at all (it is
    internal pipeline state), so a direct read is the only way to assert on it.
    Same exemption `server_channels_pending_outbound_test.py` takes.

    Counted globally rather than per channel on purpose: a simulate that
    created a binding might well file it under a channel this test never made,
    and a per-channel count would miss exactly that.
    """
    return len(db.exec(select(ChannelThreadBinding)).all())


def _agent_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    """The agents this user owns — an install would add one."""
    r = client.get(f"{API}/agents/", headers=headers)
    assert r.status_code == 200, r.text
    return {a["id"] for a in r.json()["data"]}


def _outbound_events() -> list:
    """Every outbound debug-feed entry in the process, under ANY channel key.

    EXEMPTION — the buffer is process-global class state and its HTTP surface
    (`GET /server-channels/{id}/debug-events`) can only be asked about one
    channel at a time. That is not good enough here: a hand-typed simulate has
    no channel, so anything it emitted would be filed under a key no test knows
    to ask about, and a channel-scoped assertion would pass while the reply had
    in fact been recorded.

    That is not hypothetical — the channel-scoped form of this helper was
    written first and a mutation that made `decide` record an outbound event
    sailed straight past it. The assertion looked careful and could not fail.
    Read across every key instead. The autouse `reset_channel_debug_buffer`
    fixture guarantees the buffer starts empty per test, so "every key" is
    exactly "everything this test caused".
    """
    from app.services.server_channels.channel_debug_buffer import ChannelDebugBuffer

    return [
        event
        for events in ChannelDebugBuffer._buffers.values()
        for event in events
        if event.direction == "outbound"
    ]


def _routable_user(client: TestClient, superuser_headers: dict[str, str]) -> tuple[dict, dict, dict]:
    """A user with exactly one personal route, so Pass 1 takes `only_one`.

    One route, not two: the `only_one` short-circuit means no classifier has to
    be mocked and the decision is deterministic without an LLM.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"Sim-{random_lower_string()[:6]}")
    drain_tasks()
    create_user_route(client, headers, agent["id"], trigger_prompt="Handle anything")
    return user, headers, agent


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------


def test_simulate_routes_to_an_agent_and_creates_no_binding_session_or_reply(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    A simulate that *succeeds* — it picks the user's agent — is the case where
    the real path would have bound a thread, opened a session and replied. All
    four must be absent.

    1. Target user has one installed agent reachable through Pass 1.
    2. Simulate as that user -> outcome=routed, that agent selected.
    3. No ChannelThreadBinding exists, anywhere.
    4. The target user has no sessions.
    5. The target user's agent list is unchanged.
    6. Nothing was sent outbound: no debug-feed outbound event, adapter never
       called.
    """
    # A live channel exists, so "no outbound event" is not merely "there was
    # nowhere to send one".
    create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    user, headers, agent = _routable_user(client, superuser_token_headers)

    bindings_before = _binding_count(db)
    agents_before = _agent_ids(client, headers)
    assert list_sessions(client, headers) == [], "precondition: no sessions yet"

    with patched_routing_externals() as send_mock:
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=user["id"],
        )

    # ── The decision itself really happened ───────────────────────────────
    assert trace["origin"] == "simulate"
    assert trace["outcome"] == "routed"
    assert trace["selected_agent_id"] == agent["id"]
    assert trace["user_id"] == user["id"]

    # ── ...and nothing else did ───────────────────────────────────────────
    assert _binding_count(db) == bindings_before, (
        "simulate created a channel thread binding"
    )
    assert list_sessions(client, headers) == [], "simulate created a session"
    assert _agent_ids(client, headers) == agents_before, "simulate installed an agent"
    assert _outbound_events() == [], "simulate produced an outbound channel event"
    assert send_mock.call_count == 0, "simulate sent an outbound message"


def test_simulate_over_the_catalog_parks_nothing_and_installs_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The Pass-2 case, where the real path's effect is an *install* — the
    heaviest side effect in the pipeline and the one a simulate must not have.

    1. A public bundle on the server auto-install list; a consumer who has not
       installed it.
    2. Simulate with include_catalog -> outcome=parked_install naming the
       bundle. ("parked_install" is the router's verdict vocabulary, not a
       claim that anything was parked; nothing was.)
    3. The consumer's agent list is unchanged — no install happened.
    4. Still no binding, no session, no outbound message.
    """
    create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])

    publisher_agent = create_agent_via_api(
        client, publisher_headers, name=f"Cat-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{publisher_agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle catalog requests"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, publisher_agent["id"])
    bundle_uuid = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=publisher_headers
    ).json()["bundle_uuid"]
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)

    classify_result = type("ClassifyResult", (), {"agent_id": bundle_uuid})()

    bindings_before = _binding_count(db)
    consumer_agents_before = _agent_ids(client, consumer_headers)

    with patched_routing_externals(classify_result=classify_result) as send_mock:
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="please handle this",
            as_user_id=consumer["id"],
            include_catalog=True,
        )

    assert trace["outcome"] == "parked_install"
    assert trace["selected_bundle_uuid"] == bundle_uuid

    assert _agent_ids(client, consumer_headers) == consumer_agents_before, (
        "simulate installed the catalog bundle for the consumer"
    )
    assert _binding_count(db) == bindings_before, "simulate created a binding"
    assert list_sessions(client, consumer_headers) == [], "simulate created a session"
    assert _outbound_events() == []
    assert send_mock.call_count == 0


def test_simulate_without_catalog_never_reaches_pass_two(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """`include_catalog=False` answers the narrower question, and still binds
    nothing. The stage list is the observable: only `pass_1` ran."""
    create_server_channel(
        client, superuser_token_headers, auto_register_users=True, email_whitelist="*"
    )
    consumer, consumer_headers = make_user_and_headers(client)
    bindings_before = _binding_count(db)

    with patched_routing_externals() as send_mock:
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="anybody home?",
            as_user_id=consumer["id"],
            include_catalog=False,
        )

    assert [s["stage"] for s in trace["stages"]] == ["pass_1"]
    assert trace["outcome"] == "no_match"
    assert _binding_count(db) == bindings_before
    assert list_sessions(client, consumer_headers) == []
    assert send_mock.call_count == 0


# ---------------------------------------------------------------------------
# The §12 conditions that make the exposure acceptable
# ---------------------------------------------------------------------------


def test_simulate_is_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Not even the user being simulated may run it against themselves — the
    response names other people's agents and their owners' trigger prompts, so
    it is superuser-or-nothing like every other route on this router."""
    user, headers, _ = _routable_user(client, superuser_token_headers)
    simulate_routing(
        client,
        headers,
        message="hi",
        as_user_id=user["id"],
        expected_status=403,
    )


def test_simulate_audits_the_acting_admin_and_the_target_without_the_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The audit row has to answer "which admin looked at whose routing state".
    A row saying only that *an* admin ran a simulate answers neither half of
    the question anybody asks later, so both ends are asserted here — and the
    message body is asserted *absent*, following the admin test-send precedent
    (SecurityEvent rows are broadly readable).
    """
    user, headers, _ = _routable_user(client, superuser_token_headers)
    secret = f"stimulate-{random_lower_string()}"

    with patched_routing_externals():
        simulate_routing(
            client, superuser_token_headers, message=secret, as_user_id=user["id"]
        )

    events = find_security_events(
        client, superuser_token_headers, "ROUTING_SIMULATE_RUN"
    )
    assert len(events) == 1, events
    details = events[0]["details"]
    assert details["mode"] == "simulate"
    assert details["target_user_id"] == user["id"]
    assert details["target_user_email"] == user["email"]
    assert details["message_chars"] == len(secret)
    # The body itself is nowhere in the payload, under any key.
    assert secret not in str(details), details


def test_simulate_is_rate_limited_per_admin(
    client: TestClient, superuser_token_headers: dict[str, str], monkeypatch
) -> None:
    """Each simulate costs a real LLM call, so an admin gets a bounded number
    per minute. Asserted through the route (a 429 with Retry-After), not by
    poking the limiter."""
    from app.api.routes import admin_routing

    monkeypatch.setattr(settings, "ROUTING_SIMULATE_RATE_LIMIT_PER_MIN", 2)
    # A fresh limiter: the module-level one is process-global and carries hits
    # from any earlier test in this worker, which would make the boundary this
    # test asserts depend on execution order.
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(admin_routing, "_simulate_rate_limiter", RateLimiter())

    user, _, _ = _routable_user(client, superuser_token_headers)
    with patched_routing_externals():
        for _ in range(2):
            simulate_routing(
                client, superuser_token_headers, message="hi", as_user_id=user["id"]
            )
        r = client.post(
            f"{API}/admin/routing/simulate",
            headers=superuser_token_headers,
            json={"message": "hi", "as_user_id": user["id"], "include_catalog": True},
        )
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers


def test_simulate_response_is_the_stored_trace_read_back(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Condition 4: a simulate exposes exactly what a stored trace exposes.

    Proved by fetching the same trace through `GET /traces/{id}` and comparing
    the two payloads for equality. They match because the route returns
    `RoutingTraceService.get`'s output rather than projecting anything of its
    own — if a parallel projection is ever introduced here, this goes red on
    the first field that diverges.
    """
    user, _, agent = _routable_user(client, superuser_token_headers)

    with patched_routing_externals():
        simulated = simulate_routing(
            client, superuser_token_headers, message="hello", as_user_id=user["id"]
        )

    fetched = get_routing_trace(client, superuser_token_headers, simulated["id"])
    assert simulated == fetched

    # And it is a real row on the list surface, tagged as a simulate and
    # carrying the admin who ran it — the join between the audit entry and the
    # decision it describes.
    page = list_routing_traces(client, superuser_token_headers, origin="simulate")
    ids = {row["id"] for row in page["data"]}
    assert simulated["id"] in ids
    row = next(r for r in page["data"] if r["id"] == simulated["id"])
    assert row["actor_user_id"] is not None
    assert row["actor_user_id"] != user["id"]


def test_simulate_rejects_an_unknown_user_and_an_empty_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Both refusals happen before any LLM call — an unknown target is a 404
    and an empty message is a 400, neither of which spends anything."""
    user, _, _ = _routable_user(client, superuser_token_headers)
    simulate_routing(
        client,
        superuser_token_headers,
        message="hi",
        as_user_id=str(uuid.uuid4()),
        expected_status=404,
    )
    simulate_routing(
        client,
        superuser_token_headers,
        message="   ",
        as_user_id=user["id"],
        expected_status=400,
    )


def test_simulate_points_at_the_error_trace_when_the_routing_pass_blows_up(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A failing routing pass must not surface as a bare 500.

    Found in review. `decide` re-raises whatever the pass raised — and it does
    so *after* the failing thread target has already persisted its own
    `outcome=error` row, because `capture` re-raises unchanged and the caller
    would otherwise never see that recorder. Left uncaught in
    `RoutingTuningService.simulate` that became a naked 500: the single most
    useful row this table can hold had just been written, and the admin was
    told nothing about it.

    Deliberately NOT fixed by having `decide` swallow and return. The real
    channel path depends on that exception reaching `_route_new_thread`'s
    handler to send REPLY_SETUP_FAILED, and a diagnostic route's ergonomics
    must not change what an external sender is told. So the response points at
    the row instead, and this test pins both halves: the pointer, and the row
    actually being there to point at.
    """
    from unittest.mock import patch

    from tests.utils.routing import list_routing_traces

    user, _, _ = _routable_user(client, superuser_token_headers)

    class _RouterBoom(RuntimeError):
        pass

    with patched_routing_externals(), patch(
        "app.services.server_channels.channel_routing_service."
        "ChannelRoutingService._route_installed",
        side_effect=_RouterBoom("router-blew-up"),
    ):
        r = client.post(
            f"{API}/admin/routing/simulate",
            headers=superuser_token_headers,
            json={"message": "hi", "as_user_id": user["id"], "include_catalog": True},
        )

    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert "_RouterBoom" in detail
    # De-tainted, like every other error the recorder stores: the exception's
    # own message can echo a provider payload containing the sender's words.
    assert "router-blew-up" not in detail
    assert "origin=simulate&outcome=error" in detail

    # The row the detail points at really exists, and the pointer really finds
    # it — an instruction that leads nowhere is worse than no instruction.
    page = list_routing_traces(
        client, superuser_token_headers, origin="simulate", outcome="error"
    )
    assert page["count"] == 1, page["data"]
    assert page["data"][0]["actor_user_id"] is not None
