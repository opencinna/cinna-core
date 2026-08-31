"""App MCP routing decisions write routing traces, under `ROUTING_TRACE_APP_MCP_MODE`.

Phase 6 of `docs/plans/channels_identity_unification/` gave App MCP the trace
producer it never had. `AppMCPRoutingService.route_message` now opens an
`origin="app_mcp"` capture around the whole decision and persists it, and a
per-origin setting — `ROUTING_TRACE_APP_MCP_MODE`, `off` | `metadata` | `full`,
defaulting to `metadata` — governs how much of it is written.

**The setting is why this file exists at all.** It shipped once before and was
*removed*, on the ground that nothing opened an `app_mcp` capture, so an
operator who set it to `off` would have believed they had disabled a capture
that was never running. It comes back in the same change that starts emitting
the origin, and the condition attached to it is that `off` is proved by a test
rather than by inspection. `test_mode_off_writes_no_trace_and_attempts_none`
below is that test, and it asserts *absence* rather than a swallowed failure:
`RoutingTraceService.persist` never raises — it logs and returns `None` — so
"the capture was suppressed" and "the capture ran and the write quietly failed"
would look identical from outside unless something tells them apart. That test
spies on `persist` and pins its call count at **0**, against the same scenario
at `metadata` where it is **1**.

WHY THIS CALLS `AppMCPRoutingService.route_message()` DIRECTLY
--------------------------------------------------------------
Same reason, and same precedent, as `app_mcp_identity_candidate_provider_test`
(read its equivalent heading): `tests/api/app_mcp/` has no HTTP route to drive
— App MCP is an MCP tool-call surface — and every file in this directory
already enters at the service layer, `app_mcp_session_test.py` at
`handle_send_message()` and the candidate-provider file one layer below that.
`route_message()` is the function that opens the capture, so it is the right
depth: entering at `handle_send_message()` would drag an environment stub and
the whole streaming pipeline through every test here to observe a decision
that has already finished by then. Every input to the decision — the caller,
their agents, those agents' trigger prompts — is created through real HTTP
routes, and every trace is read back through the real
`GET /api/v1/admin/routing/traces`.

NO CLASSIFIER STUB WHERE THE BALLOT HOLDS ONE CANDIDATE
--------------------------------------------------------
`tests/api/routing/README.md` is explicit that passing no classifier answer
*means* "this scenario must not classify", and that adding one "just in case"
disarms the signal. A caller who owns exactly one eligible agent takes App MCP
Stage 1's `only_one` short-circuit and never reaches a model; the autouse
`block_llm_provider` guard would raise `UnstubbedLLMProvider` (a
`BaseException`, so no `except Exception` in the code under test can swallow
it) if one of these scenarios started classifying. The two tests that need a
real prompt and a real reply on the trace say so by giving their caller **two**
eligible agents and patching
`app.services.routing.agent_classifier.get_provider_manager` themselves — one
layer deeper than `AgentClassifier.classify`, because that is where
`record_prompt` / `record_raw_response` fire, and a test that stubbed
`classify` would find those fields absent whatever the mode said (see that
README's note on the same trap in the channel domain).
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import (
    ROUTING_TRACE_APP_MCP_FULL,
    ROUTING_TRACE_APP_MCP_METADATA,
    ROUTING_TRACE_APP_MCP_OFF,
)
from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
from app.services.routing.routing_trace_service import RoutingTraceService
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.routing import (
    get_routing_trace,
    list_routing_traces,
    seed_routing_trace,
)
from tests.utils.server_channel import find_server_channel_by_type, list_debug_events
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

_APP_MCP = "app_mcp"
_MODE_SETTING = "app.core.config.settings.ROUTING_TRACE_APP_MCP_MODE"
_TEXT_SETTING = "app.core.config.settings.ROUTING_TRACE_STORE_MESSAGE_TEXT"
#: One layer BELOW `AgentClassifier.classify` — see the module docstring.
_PROVIDER_TARGET = "app.services.routing.agent_classifier.get_provider_manager"


# ── Setup ────────────────────────────────────────────────────────────────────


def _caller(client: TestClient, superuser_headers: dict[str, str]) -> tuple[dict, dict]:
    """A developer with their own AI credential — the App MCP caller."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _eligible_agent(client: TestClient, headers: dict[str, str], label: str) -> dict:
    """An agent that `ChannelCandidateProvider` will admit to the ballot.

    A trigger prompt is what makes an agent a candidate — there is no route row
    to create any more (the `AppAgentRoute` family was deleted in phase 5).
    """
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, headers, agent["id"], f"Handle {label} work for the caller"
    )
    return agent


def _app_mcp_trace_ids(client: TestClient, superuser_headers: dict[str, str]) -> set[str]:
    page = list_routing_traces(client, superuser_headers, origin=_APP_MCP)
    return {row["id"] for row in page["data"]}


def _all_trace_ids(client: TestClient, superuser_headers: dict[str, str]) -> set[str]:
    page = list_routing_traces(client, superuser_headers, limit=200)
    return {row["id"] for row in page["data"]}


def _the_one_new_trace(
    client: TestClient, superuser_headers: dict[str, str], before: set[str]
) -> dict:
    """The single `app_mcp` row that appeared since `before`.

    Diffed rather than indexed into: `GET /admin/routing/traces` orders by
    `created_at DESC, id DESC` and the tiebreak is a random UUID, so `data[0]`
    is not a stable way to name "the row this decision just wrote" — a fact
    `tests/api/routing/README.md` records having already bitten once.
    """
    after = _app_mcp_trace_ids(client, superuser_headers)
    new = after - before
    assert len(new) == 1, f"expected exactly one new app_mcp trace, got {new}"
    page = list_routing_traces(client, superuser_headers, origin=_APP_MCP)
    return next(row for row in page["data"] if row["id"] in new)


def _classify_reply(ref_id: str) -> MagicMock:
    """The provider's response object; `.text` is what `classify` reads."""
    response = MagicMock()
    response.text = json.dumps({"agent_id": ref_id})
    return response


# ── 1. The producer ──────────────────────────────────────────────────────────


def test_an_app_mcp_decision_writes_a_trace_with_the_singleton_channel_id(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The headline: App MCP was the last silent surface, and it is not one now.

    `channel_id` is the half worth stating separately. App MCP is a singleton
    `ServerChannel`, so "which channel was this decision made on" has a real
    answer here — unlike a hand-typed simulate, which carries NULL by design —
    and the trace must carry it, or a channel-scoped read of the App MCP
    channel would show nothing while App MCP was routing.

    The caller owns exactly one eligible agent, so this decision takes Stage
    1's `only_one` short-circuit and names no classifier answer. See the module
    docstring: that silence is an assertion.
    """
    user, headers = _caller(client, superuser_token_headers)
    agent = _eligible_agent(client, headers, "solo")
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)

    before = _app_mcp_trace_ids(client, superuser_token_headers)

    result = AppMCPRoutingService.route_message(
        db, uuid.UUID(user["id"]), "please handle this for me"
    )

    assert result is not None, "the sole eligible agent should have been picked"
    assert str(result.agent_id) == agent["id"]

    row = _the_one_new_trace(client, superuser_token_headers, before)
    assert row["origin"] == _APP_MCP
    assert row["channel_id"] == channel["id"], row
    assert row["user_id"] == user["id"], row
    assert row["outcome"] == "routed", row
    assert row["selected_agent_id"] == agent["id"], row
    assert row["match_method"] == "only_one", row
    # Simulate's field, and nobody stands behind an App MCP call.
    assert row["actor_user_id"] is None, row

    detail = get_routing_trace(client, superuser_token_headers, row["id"])
    stage = next(s for s in detail["stages"] if s["stage"] == "pass_1")
    # The recorder calls already in this service — `record_match`,
    # `record_candidate` inside the provider — were no-ops before a capture
    # existed. Opening one made them all live at once; this is that.
    assert [c["ref_id"] for c in stage["candidates"]] == [agent["id"]], stage


def test_a_decision_that_matches_nothing_still_writes_its_trace(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """A caller with an empty ballot is exactly the decision an admin opens a
    trace to explain — "why didn't it find my agent" — so it must leave a row.

    No classifier answer, again deliberately: an empty candidate list
    short-circuits before the model is ever reached.
    """
    user, _headers = _caller(client, superuser_token_headers)
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)

    before = _app_mcp_trace_ids(client, superuser_token_headers)

    result = AppMCPRoutingService.route_message(
        db, uuid.UUID(user["id"]), "is anybody there"
    )

    assert result is None
    row = _the_one_new_trace(client, superuser_token_headers, before)
    assert row["outcome"] == "no_match", row
    assert row["channel_id"] == channel["id"], row
    assert row["selected_agent_id"] is None, row


# ── 2. `off` — the ruling's hard condition ───────────────────────────────────


def test_mode_off_writes_no_trace_and_attempts_none(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`ROUTING_TRACE_APP_MCP_MODE="off"` suppresses the App MCP capture.

    Four things are asserted, and the last three are why this is not a one-line
    test:

    1. **No row.** Not for `origin="app_mcp"`, and not anywhere — the whole
       trace table is diffed, so an `outcome="error"` row standing in for the
       suppressed one would fail here too.
    2. **Nothing attempted.** `RoutingTraceService.persist` is spied on and its
       call count pinned at `0`. This is the assertion that distinguishes
       *absence* from *a quiet failure wearing absence's clothes*: `persist`
       swallows its own exceptions and returns `None`, so a capture that opened
       and then failed to write would produce exactly the same empty table.
       An operator setting `off` is entitled to the first, not the second.
    3. **The spy is not vacuous.** The identical scenario at the `metadata`
       default calls `persist` exactly once, asserted in the same test — so a
       spy that could never fire, or a helper that never routes at all, fails
       instead of passing quietly.
    4. **Routing still works.** `off` is a diagnostics switch, not a kill
       switch; the decision itself is unchanged and still picks the agent.

    Deliberately **not** asserted through `caplog`. "Nothing was logged" is the
    forbidden shape in `tests/api/routing/README.md` — `alembic`'s
    `fileConfig(disable_existing_loggers=True)` makes a `not in caplog.text`
    guard pass vacuously and forever. The `persist` spy states the same fact
    positively, and states a stronger one: not that a failure went unlogged,
    but that no write was ever begun.
    """
    user, headers = _caller(client, superuser_token_headers)
    agent = _eligible_agent(client, headers, "off-mode")

    before_app_mcp = _app_mcp_trace_ids(client, superuser_token_headers)
    before_all = _all_trace_ids(client, superuser_token_headers)

    with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_OFF):
        with patch.object(
            RoutingTraceService, "persist", wraps=RoutingTraceService.persist
        ) as spy:
            result = AppMCPRoutingService.route_message(
                db, uuid.UUID(user["id"]), "route me but record nothing"
            )
            assert spy.call_count == 0, (
                "the capture must not be opened at all under `off` — a persist "
                "call means it was, and `persist` swallowing its own failure "
                "would make that indistinguishable from suppression"
            )

    # 4: the decision is untouched.
    assert result is not None and str(result.agent_id) == agent["id"]

    # 1: no row, under any origin.
    assert _app_mcp_trace_ids(client, superuser_token_headers) == before_app_mcp
    assert _all_trace_ids(client, superuser_token_headers) == before_all

    # 3: the same scenario at the default writes exactly one, through the same
    #    spy — the falsifier for everything above.
    with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_METADATA):
        with patch.object(
            RoutingTraceService, "persist", wraps=RoutingTraceService.persist
        ) as spy:
            AppMCPRoutingService.route_message(
                db, uuid.UUID(user["id"]), "route me and record it"
            )
            assert spy.call_count == 1, spy.call_args_list

    assert len(_app_mcp_trace_ids(client, superuser_token_headers) - before_app_mcp) == 1


def test_mode_off_leaves_nothing_on_the_channel_debug_feed_either(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """Suppression must not be papered over by the *other* diagnostic surface.

    The live debug feed (`GET /admin/server-channels/{id}/debug-events`) is a
    separate in-memory buffer with its own retention story, and an operator who
    switched App MCP tracing off would not expect the same decision to turn up
    there instead. App MCP touches `ChannelDebugBuffer` on no path today, which
    is what makes this cheap to state — and stating it is what keeps a future
    "well, we could at least record it here" from landing without a decision.
    """
    user, headers = _caller(client, superuser_token_headers)
    _eligible_agent(client, headers, "off-debug")
    channel = find_server_channel_by_type(client, superuser_token_headers, _APP_MCP)

    before = list_debug_events(client, superuser_token_headers, channel["id"])

    with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_OFF):
        AppMCPRoutingService.route_message(
            db, uuid.UUID(user["id"]), "nothing should record this"
        )

    after = list_debug_events(client, superuser_token_headers, channel["id"])
    assert len(after["events"]) == len(before["events"]), after


def test_mode_off_also_refuses_a_trace_that_reaches_the_write_gate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The second half of the belt-and-braces, exercised on its own.

    The producer's short-circuit is the cheap path; `RoutingTraceService
    .persist`'s refusal is the invariant. They are not redundant — a *second*
    App MCP producer added later inherits the write gate without having to
    remember the short-circuit — but the producer test above can never reach
    the gate, because it stops one layer earlier. `seed_routing_trace` (this
    domain's documented Rule-1 exemption, in `tests/utils/routing.py`) hands
    `persist` an `origin="app_mcp"` trace directly, which is exactly the shape
    that hypothetical second producer would have.
    """
    user, _headers = _caller(client, superuser_token_headers)
    before = _app_mcp_trace_ids(client, superuser_token_headers)

    with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_OFF):
        trace_id = seed_routing_trace(
            created_at=datetime.now(UTC),
            origin=_APP_MCP,
            user_id=user["id"],
            outcome="no_match",
            message="written straight at the write gate",
        )

    assert trace_id is None, "persist must refuse an app_mcp trace under `off`"
    assert _app_mcp_trace_ids(client, superuser_token_headers) == before

    # Falsifier: the same seed at `full` does land, so the assertion above is
    # about the mode and not about the helper.
    with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_FULL):
        seeded = seed_routing_trace(
            created_at=datetime.now(UTC),
            origin=_APP_MCP,
            user_id=user["id"],
            outcome="no_match",
            message="written straight at the write gate",
        )
    assert seeded is not None
    assert str(seeded) in _app_mcp_trace_ids(client, superuser_token_headers)


# ── 3. `metadata` vs `full` ──────────────────────────────────────────────────


def test_metadata_withholds_the_message_text_but_keeps_the_hash(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The default, with the *global* text flag deliberately ON.

    That combination is the whole point of a per-origin mode: the server is
    storing message text for every other origin, and App MCP alone is not.
    What survives is `message_sha256`, and it must — it is what keeps the row
    replayable and what lets the read side tell "withheld" apart from "there
    was no message" (both `NULL` would be indistinguishable).
    """
    user, headers = _caller(client, superuser_token_headers)
    _eligible_agent(client, headers, "metadata")
    text = f"metadata-mode-{random_lower_string()}"
    expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    before = _app_mcp_trace_ids(client, superuser_token_headers)
    with patch(_TEXT_SETTING, True):
        with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_METADATA):
            AppMCPRoutingService.route_message(db, uuid.UUID(user["id"]), text)

    row = _the_one_new_trace(client, superuser_token_headers, before)
    assert row["message_text"] is None, row
    assert row["message_sha256"] == expected_sha, row


def test_full_stores_the_text_that_metadata_withholds(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`full` means "like any other origin", and the pair is the assertion.

    Without this, the test above would pass just as well against a producer
    that had stopped capturing the message at all.
    """
    user, headers = _caller(client, superuser_token_headers)
    _eligible_agent(client, headers, "full")
    text = f"full-mode-{random_lower_string()}"

    before = _app_mcp_trace_ids(client, superuser_token_headers)
    with patch(_TEXT_SETTING, True):
        with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_FULL):
            AppMCPRoutingService.route_message(db, uuid.UUID(user["id"]), text)

    row = _the_one_new_trace(client, superuser_token_headers, before)
    assert row["message_text"] == text, row


def test_the_two_flags_and_rather_than_override_each_other(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """`ROUTING_TRACE_STORE_MESSAGE_TEXT=False` wins even at mode `full`.

    The regression a two-flag arrangement invites is a permissive origin mode
    re-opening a closed privacy gate. `metadata` narrows and never widens, so
    the truth table has exactly one "yes" — global on AND mode `full` — and
    this pins the corner that would be wrong if the App MCP branch had been
    written as `store_text = mode == "full"` rather than as a narrowing of the
    value the global flag already produced.

    Applied at the **write**, not the read: with the flag off the text is not
    in the database at all, so re-reading it with the flag back on cannot
    produce it. Asserted by flipping the read back on below.
    """
    user, headers = _caller(client, superuser_token_headers)
    _eligible_agent(client, headers, "and-gate")
    text = f"and-gate-{random_lower_string()}"
    expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    before = _app_mcp_trace_ids(client, superuser_token_headers)
    with patch(_TEXT_SETTING, False):
        with patch(_MODE_SETTING, ROUTING_TRACE_APP_MCP_FULL):
            AppMCPRoutingService.route_message(db, uuid.UUID(user["id"]), text)

    # Read with the gate back OPEN: anything served now came out of the row.
    row = _the_one_new_trace(client, superuser_token_headers, before)
    assert row["message_text"] is None, (
        "mode `full` must not re-open a text gate the global flag closed — and "
        "since this read runs with the global flag ON, a non-None value here "
        "would mean the text really was written to the database"
    )
    assert row["message_sha256"] == expected_sha, row


def test_metadata_withholds_the_prompt_and_raw_response_while_candidates_survive(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """The stage-level half of `metadata`, on fields that genuinely have
    something to hide.

    Two eligible agents, so Stage 1 really classifies, and the provider is
    stubbed one layer BELOW `AgentClassifier.classify` so the real render and
    the real parse run and `record_prompt` / `record_raw_response` actually
    fire. Stubbing `classify` itself would leave both fields `None` whatever
    the mode said — the test would assert the instrument, not the gate.

    Run twice, `full` then `metadata`, because the suppression assertion is
    only meaningful next to a decision where the same two fields are present.
    What survives on both is `candidates` — the agent owner's own
    configuration, on `SAFE_CANDIDATE_FIELDS` — which is what makes `metadata`
    a narrowing of the trace rather than a deletion of it.
    """
    user, headers = _caller(client, superuser_token_headers)
    first = _eligible_agent(client, headers, "ballot-one")
    second = _eligible_agent(client, headers, "ballot-two")
    assert second["id"] != first["id"]

    def _decide_under(mode: str) -> dict:
        before = _app_mcp_trace_ids(client, superuser_token_headers)
        with patch(_TEXT_SETTING, True):
            with patch(_MODE_SETTING, mode):
                with patch(_PROVIDER_TARGET) as provider:
                    provider.return_value.generate_content.return_value = (
                        _classify_reply(first["id"])
                    )
                    result = AppMCPRoutingService.route_message(
                        db, uuid.UUID(user["id"]), "classify this properly"
                    )
                    # Proof the REAL classifier ran rather than a stubbed
                    # verdict: the provider was actually called.
                    assert provider.return_value.generate_content.call_count == 1
        assert result is not None and str(result.agent_id) == first["id"]
        row = _the_one_new_trace(client, superuser_token_headers, before)
        return get_routing_trace(client, superuser_token_headers, row["id"])

    full_detail = _decide_under(ROUTING_TRACE_APP_MCP_FULL)
    full_stage = next(s for s in full_detail["stages"] if s["stage"] == "pass_1")
    # Non-empty rather than content-matched: the recorder clamps every free-text
    # field to `TRACE_TEXT_MAX_CHARS` before any flag is consulted, and the
    # static prompt template is longer than that on its own, so the stored
    # `prompt` is always a truncated slice of it. `raw_response` is short
    # enough to survive whole, so that one can pin content.
    assert full_stage["prompt"], full_stage
    assert first["id"] in full_stage["raw_response"], full_stage
    assert full_stage["match_method"] == "ai", full_stage

    meta_detail = _decide_under(ROUTING_TRACE_APP_MCP_METADATA)
    meta_stage = next(s for s in meta_detail["stages"] if s["stage"] == "pass_1")
    # `.get`, not `[...]`: a gated stage is projected through an ALLOWLIST, so a
    # withheld field is absent rather than present-and-blanked.
    assert meta_stage.get("prompt") is None, meta_stage
    assert meta_stage.get("raw_response") is None, meta_stage
    # ...while the diagnosis that needs no sender text comes through untouched.
    assert meta_stage["match_method"] == "ai", meta_stage
    assert {c["ref_id"] for c in meta_stage["candidates"]} == {
        first["id"],
        second["id"],
    }, meta_stage
    assert all(c["trigger_prompt"] for c in meta_stage["candidates"]), meta_stage
