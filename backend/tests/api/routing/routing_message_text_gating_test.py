"""`ROUTING_TRACE_STORE_MESSAGE_TEXT` both ways, through the real channel path.

Per the developer's Phase 2 notes: `origin` is always `server_channel` today
(no call site opens a capture for `app_mcp` / `identity` / `simulate`), but
`ROUTING_TRACE_STORE_MESSAGE_TEXT` IS testable both ways through the channel
path — no need to reach for the `app_mcp` per-origin mode to prove the text
gate.

The property that matters most (plan §7): with the flag off, `message_text`
must be absent, but `message_sha256` must still be present — that residue is
what makes the trace usable for replay/dedupe even with text storage
disabled. Proven here by computing the expected hash independently
(`hashlib.sha256`) and asserting the stored value matches exactly, in BOTH
flag states, with the same input text — the hash must not itself depend on
whether the text is stored.

**The S5 gap this file used to have** (found auditing after a later dev pass
added `stages[].prompt` / `raw_response` gating): every test below —
and every other test in this domain that reaches Pass 2 — mocks
`AgentClassifier.classify` directly via `post_channel_message`'s
`classify_result` / `classify_side_effect`. That boundary sits ABOVE the only
code that ever calls `routing_trace.record_prompt` / `record_raw_response`
(inside `AgentClassifier.classify` itself, one layer further down). Mocking at
the higher boundary means those two stage fields are `None` for every trace
this suite ever produces, regardless of the flag — so a test asserting "prompt
is absent when the flag is off" was, and is, capable of passing against a
broken scrub (or a broken UN-scrub) because there was never a prompt to leak
in the first place.
`test_stage_prompt_and_raw_response_are_read_gated_by_store_message_text`
below closes that gap by mocking one layer deeper
(`app.services.routing.agent_classifier.get_provider_manager`), so the real
render/parse genuinely runs and genuinely populates both fields before the
gate is exercised against them.

Both targets moved in Phase 5, which unified three copies of the candidate
builder behind `AgentClassifier`: the classifier boundary was
`AIFunctionsService.route_to_agent` and the provider seam was
`app.agents.app_agent_router.get_provider_manager`.
"""
import hashlib
import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.routing import get_routing_trace, list_routing_traces, post_channel_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SETTING = "app.core.config.settings.ROUTING_TRACE_STORE_MESSAGE_TEXT"
_PROVIDER_TARGET = "app.services.routing.agent_classifier.get_provider_manager"


def _channel(client, superuser_headers) -> dict:
    return create_server_channel(client, superuser_headers, auto_register_users=True, email_whitelist="*")


def _post_and_get_row(client, superuser_headers, channel, text: str) -> dict:
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text=text, sender_email=f"{random_lower_string()}@example.com")
    resp, _ = post_channel_message(client, channel, signer, event)
    assert resp.status_code == 200
    page = list_routing_traces(client, superuser_headers, channel_id=channel["id"])
    assert page["count"] == 1
    return page["data"][0]


def test_message_text_stored_when_flag_is_on(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    text = f"text-on-{random_lower_string()}"
    expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    with patch(_SETTING, True):
        row = _post_and_get_row(client, superuser_token_headers, channel, text)

    assert row["message_text"] == text
    assert row["message_sha256"] == expected_sha


def test_message_text_absent_but_sha256_still_present_when_flag_is_off(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The property the whole flag exists to prove: text off does not mean
    the trace is useless — the replay/dedupe key survives."""
    channel = _channel(client, superuser_token_headers)
    text = f"text-off-{random_lower_string()}"
    expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    with patch(_SETTING, False):
        row = _post_and_get_row(client, superuser_token_headers, channel, text)

    assert row["message_text"] is None
    assert row["message_sha256"] == expected_sha
    assert row["message_sha256"] is not None and len(row["message_sha256"]) == 64


# ── S5: stages[].prompt / raw_response, exercised through a REAL classify ──


def _classify_response(agent_id: str) -> MagicMock:
    """Stub for the LLM provider's response object (`.text` is what
    `app_agent_router.route_to_agent` reads)."""
    resp = MagicMock()
    resp.text = json.dumps({"agent_id": agent_id})
    return resp


def _publish_catalog_bundle(client, superuser_headers) -> str:
    """A published, public, catalog-listed bundle with a real trigger prompt
    — Pass 2's one eligible candidate. Returns its `bundle_uuid`."""
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_headers, publisher["id"])
    agent = create_agent_via_api(client, publisher_headers, name=f"S5-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle S5 prompt-capture requests"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    bundle_uuid = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()["bundle_uuid"]
    add_auto_install_bundle(client, superuser_headers, bundle_uuid)
    return bundle_uuid


def test_stage_prompt_and_raw_response_are_read_gated_by_store_message_text(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Write one row with the flag ON, via a REAL Pass 2 classify call (see
       module docstring for why this must not mock `AIFunctionsService
       .route_to_agent` directly) — `pass_2`'s stage gets a real, non-empty
       `prompt` and a real `raw_response` naming the chosen bundle.
    2. Read that row back with the flag still ON: both fields are visible.
    3. Read the SAME row with the flag now OFF: both are scrubbed to `None`
       on read — proving the read gate against a row that genuinely had
       something to hide, and proving §7's "hides, does not erase": nothing
       was rewritten, the very next read with the flag back on would show
       the same text again.

    **Why this doesn't assert the raw message text is IN the stored
    `prompt`** (a first draft of this test did, and it failed — not a
    production bug): the recorder clamps every free-text field to
    `TRACE_TEXT_MAX_CHARS` (2000 chars) BEFORE `ROUTING_TRACE_STORE_MESSAGE_TEXT`
    is ever consulted, and `app_agent_router_prompt.md` — the static template
    prefixed onto every rendered prompt — is already ~2300 chars on its own.
    So the stored `prompt` is always a truncated slice of the static template
    text; the "## User Message" section holding the sender's actual words is
    appended AFTER the template and never survives the clamp. That is a
    genuine, load-bearing fact about this feature's exposure profile (the
    trace is clamped independent of the flag), not a test bug to work around
    by asserting less. What this test instead does to prove the REAL
    function ran (not a higher-layer classify-result mock) is inspect the
    mock's own `call_args` — the unclamped prompt actually handed to the LLM
    provider — for the sender's message, while asserting only that the
    STORED, gated `prompt` is non-empty/absent as appropriate.
    """
    channel = _channel(client, superuser_token_headers)
    bundle_uuid = _publish_catalog_bundle(client, superuser_token_headers)

    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    text = f"please help {random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text=text, sender_email=f"{random_lower_string()}@example.com")

    with patch(_SETTING, True):
        with patch(_PROVIDER_TARGET) as mock_pm:
            mock_pm.return_value.generate_content.return_value = _classify_response(bundle_uuid)
            resp, _ = post_channel_message(
                client, channel, signer, event, classify_via_provider=True
            )
            # Proof this test reaches the REAL `app_agent_router.route_to_agent`
            # rather than a higher-layer classify-result mock: the sender's
            # actual message is present in the UNCLAMPED prompt the provider
            # was actually called with (the stored/gated copy is clamped away
            # before the message ever appears in it — see the docstring above).
            assert mock_pm.return_value.generate_content.call_count >= 1
            sent_prompt = mock_pm.return_value.generate_content.call_args[0][0]
            assert text in sent_prompt, sent_prompt
        assert resp.status_code == 200

        page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
        assert page["count"] == 1, page["data"]
        row = page["data"][0]
        # Sanity: the REAL classify actually matched. `match_method` is what
        # says so — it survives a non-routed outcome by design (see
        # `RoutingDecision.match_method`'s column docs: "how the last stage
        # matched", not "how the decision was reached").
        #
        # The verdict is `error`, not `parked_install`, and that is correct: the
        # classifier picked the bundle and `_install_and_park` then failed for
        # this auto-registered sender, who is told "setting up your assistant
        # failed". This assertion used to read `outcome == "parked_install"` and
        # passed — because the row was persisted BEFORE `_install_and_park` ran,
        # so it durably claimed an install that never completed, with
        # `error=NULL` and invisible to `?outcome=error`. The persist now happens
        # after the effect it describes, and the row says what really happened.
        assert row["match_method"] == "ai", row
        assert row["outcome"] == "error", row
        assert row["error"] is not None, row
        row_id = row["id"]

        detail_on = get_routing_trace(client, superuser_token_headers, row_id)

    pass2_on = next(s for s in detail_on["stages"] if s["stage"] == "pass_2")
    # Non-empty, not a literal-content match — see the docstring: the stored
    # copy is clamped to the (longer-than-2000-char) static template prefix,
    # never the sender's message. Non-emptiness is exactly the S5 property
    # under test: something real was captured for the read gate to act on.
    assert pass2_on["prompt"], pass2_on
    assert pass2_on["raw_response"], pass2_on
    # `raw_response` IS short enough to survive the clamp whole (it's just
    # the LLM's `{"agent_id": ...}` reply), so this one CAN pin exact content.
    assert bundle_uuid in pass2_on["raw_response"]

    with patch(_SETTING, False):
        detail_off = get_routing_trace(client, superuser_token_headers, row_id)

    pass2_off = next(s for s in detail_off["stages"] if s["stage"] == "pass_2")
    # `.get`, not `[...]`: with the gate off a stage is projected through an
    # ALLOWLIST (`routing_trace.SAFE_STAGE_FIELDS`), so a withheld field is
    # absent rather than present-and-blanked. That is the shape change the
    # inversion makes — and the point of it: a field nobody declared safe is not
    # served, including one added after this gate was written.
    assert pass2_off.get("prompt") is None, pass2_off
    assert pass2_off.get("raw_response") is None, pass2_off
    # ...while the diagnosis that needs no sender text survives it untouched.
    assert pass2_off["candidates"], pass2_off
    assert pass2_off.get("match_method") == pass2_on.get("match_method")


# ── `stages[].reason` left the allowlist when the model started filling it ──


def test_model_supplied_reason_is_withheld_when_message_text_is_gated(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`stages[].reason` is sender text now, and the gate must treat it as such.

    The field was on `SAFE_STAGE_FIELDS` for the whole of Phases 1–4, and was
    safe there: nothing but our own parse literals ("classifier reply was not
    JSON") ever reached it. Phase 5's prompt contract asks the classifier for a
    `reason`, and a model explaining its choice quotes the message back — so the
    field became a rewrite of the sender's words, exactly like `raw_response`.
    It came off the allowlist in the same change that started populating it.

    Proven end-to-end against a row that genuinely had something to hide: the
    provider is mocked at classifier depth, so the REAL parse runs and the real
    reason lands in the stage. Then, with the flag on, the reason is served;
    with the flag off, it is absent — and `confidence` / `runner_up_id`, which
    are a number and a candidate id and carry nothing the sender wrote, survive
    the same projection. A test that only asserted the withholding would pass
    against an allowlist that had been emptied.
    """
    channel = _channel(client, superuser_token_headers)
    bundle_uuid = _publish_catalog_bundle(client, superuser_token_headers)

    sender_words = f"my printer is on fire {random_lower_string()}"
    reason = f"the sender said {sender_words}, which is an ops problem"

    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text=sender_words,
        sender_email=f"{random_lower_string()}@example.com",
    )

    reply = MagicMock()
    reply.text = json.dumps(
        {
            "agent_id": bundle_uuid,
            "message": None,
            "confidence": 0.91,
            "reason": reason,
            "runner_up": "NONE",
        }
    )

    with patch(_SETTING, True):
        with patch(_PROVIDER_TARGET) as mock_pm:
            mock_pm.return_value.generate_content.return_value = reply
            resp, _ = post_channel_message(
                client, channel, signer, event, classify_via_provider=True
            )
            assert mock_pm.return_value.generate_content.call_count >= 1
        assert resp.status_code == 200

        page = list_routing_traces(client, superuser_token_headers, channel_id=channel["id"])
        assert page["count"] == 1, page["data"]
        row_id = page["data"][0]["id"]
        detail_on = get_routing_trace(client, superuser_token_headers, row_id)

    pass2_on = next(s for s in detail_on["stages"] if s["stage"] == "pass_2")
    # The model's own words reached the stage — otherwise the gate below has
    # nothing to act on and this test is vacuous.
    assert pass2_on["reason"] == reason, pass2_on
    assert sender_words in pass2_on["reason"]
    assert pass2_on["confidence"] == 0.91, pass2_on

    with patch(_SETTING, False):
        detail_off = get_routing_trace(client, superuser_token_headers, row_id)

    pass2_off = next(s for s in detail_off["stages"] if s["stage"] == "pass_2")
    # `.get`, not `[...]`: an allowlist withholds by omission.
    assert pass2_off.get("reason") is None, pass2_off
    # ...and the projection did not simply collapse. `confidence` is a number
    # and stays; a passing test with an empty allowlist would be no evidence.
    assert pass2_off.get("confidence") == 0.91, pass2_off
    assert pass2_off.get("candidates"), pass2_off


# ── The allowlist: a stage field nobody declared safe is not served ────────


_ATTEMPT_TARGET = "app.services.routing.routing_trace"


def test_llm_attempt_error_is_neither_stored_nor_served_when_text_is_gated(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`stages[].llm_attempts[].error` is the third field found carrying the
    sender's words outside a gate believed complete (after `message_text`, then
    `stages[].prompt` / `raw_response`). Provider SDK exceptions routinely echo
    the request payload back, and at the router's call site that payload is the
    rendered classifier prompt.

    Two independent properties, proved in the two flag directions so that
    neither can be satisfied by the other:

      1. **Write path.** With the flag OFF while the trace is captured, the
         attempt's `error` is never stored — asserted by reading the row back
         with the flag ON, so the read projection cannot be what hides it.
      2. **Read path.** With the flag ON at capture time the error IS stored;
         reading with the flag OFF must not serve it, while
         provider/model/ok/latency_ms — the outage diagnosis that works without
         any of the sender's text — must still come through.

    Attempts are recorded by `ProviderManager._note_attempt`, which the provider
    mock below stands in for: this domain has no real LLM, and patching
    `get_provider_manager` (the depth the S5 test established) replaces the very
    object that would call the instrumentation. The side effect therefore fires
    `record_llm_attempt` itself, with a marker in `error` playing the part of an
    SDK exception that echoed the prompt back.
    """
    from app.services.routing import routing_trace

    channel = _channel(client, superuser_token_headers)
    bundle_uuid = _publish_catalog_bundle(client, superuser_token_headers)
    marker = f"SENDER-WORDS-{random_lower_string()}"

    def _post(store_text: bool) -> str:
        signer = GoogleChatJWTSigner()
        thread_key = f"spaces/AAA/threads/{random_lower_string()}"
        event = build_message_event(
            thread_key=thread_key,
            text=f"please help {random_lower_string()}",
            sender_email=f"{random_lower_string()}@example.com",
        )

        def _classify_and_note_attempt(*args, **kwargs):
            routing_trace.record_llm_attempt(
                provider="stub-provider",
                model="stub-model",
                ok=False,
                error=f"429 rate limited on request: {marker}",
                latency_ms=17,
            )
            return _classify_response(bundle_uuid)

        with patch(_SETTING, store_text):
            with patch(_PROVIDER_TARGET) as mock_pm:
                mock_pm.return_value.generate_content.side_effect = (
                    _classify_and_note_attempt
                )
                resp, _ = post_channel_message(
                    client, channel, signer, event, classify_via_provider=True
                )
            assert resp.status_code == 200
        page = list_routing_traces(
            client, superuser_token_headers, channel_id=channel["id"]
        )
        return page["data"][0]["id"]

    def _attempts(detail: dict) -> list[dict]:
        pass2 = next(s for s in detail["stages"] if s["stage"] == "pass_2")
        return pass2["llm_attempts"]

    # ── 1. Captured with the gate OFF, read back with the gate ON ──────────
    written_off_id = _post(store_text=False)
    with patch(_SETTING, True):
        detail = get_routing_trace(client, superuser_token_headers, written_off_id)
    attempts = _attempts(detail)
    assert attempts, detail
    # Read gate is OPEN here, so anything visible is what was actually stored.
    assert marker not in json.dumps(detail), detail
    assert attempts[0].get("error") in (None, ""), attempts

    # ── 2. Captured with the gate ON: stored, and served while it stays on ──
    written_on_id = _post(store_text=True)
    with patch(_SETTING, True):
        detail_on = get_routing_trace(client, superuser_token_headers, written_on_id)
    attempts_on = _attempts(detail_on)
    assert marker in attempts_on[0]["error"], attempts_on

    # ── ...and withheld the moment the gate closes, while the outage
    #    diagnosis that needs no sender text survives it untouched. ─────────
    with patch(_SETTING, False):
        detail_off = get_routing_trace(client, superuser_token_headers, written_on_id)
    attempts_off = _attempts(detail_off)
    assert marker not in json.dumps(detail_off), detail_off
    assert attempts_off[0].get("error") in (None, ""), attempts_off
    assert attempts_off[0]["provider"] == "stub-provider"
    assert attempts_off[0]["model"] == "stub-model"
    assert attempts_off[0]["ok"] is False
    assert attempts_off[0]["latency_ms"] == 17


# ── The allowlist admits the agent OWNER's own configuration, on purpose ───


def _agent_with_examples_route(
    client, headers, *, trigger_prompt: str, prompt_examples: str, name_prefix: str
) -> dict:
    """An agent plus a route assigned to its own owner, carrying both fields.

    Deliberately an *admin-shaped* route (`POST /agents/{id}/app-mcp-routes/`
    with `activate_for_myself`) rather than the personal route
    `tests.utils.app_agent_route.create_user_route` builds: only the former's
    `prompt_examples` reaches the `EffectiveRoute` that
    `record_effective_routes` reads. `AppAgentRouteService
    .get_effective_routes_for_user` sets `prompt_examples=` on the assigned-route
    branch and does NOT on the personal-route branch, so a personal route would
    make this test assert `prompt_examples is None` and prove nothing at all.
    """
    agent = create_agent_via_api(
        client, headers, name=f"{name_prefix}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.post(
        f"{API}/agents/{agent['id']}/app-mcp-routes/",
        headers=headers,
        json={
            "name": f"route-{random_lower_string()[:8]}",
            "agent_id": agent["id"],
            "session_mode": "conversation",
            "trigger_prompt": trigger_prompt,
            "prompt_examples": prompt_examples,
            "channel_app_mcp": True,
            "is_active": True,
            "activate_for_myself": True,
        },
    )
    assert r.status_code == 200, r.text
    return agent


def test_candidate_owner_config_survives_the_gate_while_sender_text_does_not(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`candidates[].trigger_prompt` / `candidates[].prompt_examples` are on the
    allowlist; `stages[].prompt`, `stages[].raw_response` and
    `llm_attempts[].error` are not — and the difference is not a matter of
    taste. The first two are the **agent owner's** configuration, which nothing
    the sender types can reach; the last three are the sender's own words, or a
    rewrite of them. Plan §7 admits the pair for exactly that reason (§9's
    near-miss verdict scores a Jaccard overlap against the trigger prompt, so
    withholding it degraded a diagnosis unrelated to sender privacy).

    Proved through the API, by execution, against a trace that genuinely
    carries all five fields — never by reading `SAFE_CANDIDATE_FIELDS` back,
    which would pass just as happily against a projection that ignores it.

    **Pass 1, with two routes.** One route takes the `only_one` short-circuit
    and never classifies, so there would be no prompt / raw response for the
    gate to act on. Two forces `AppMCPRoutingService._ai_classify`, and the
    provider patch is
    `app.services.routing.agent_classifier.get_provider_manager` —
    the depth the S5 test above established — so the real classifier runs
    and the real `record_prompt` / `record_raw_response` fire. The provider stub
    also calls `record_llm_attempt` itself, standing in for
    `ProviderManager._note_attempt` (which the patch replaces), with a marker in
    `error` playing an SDK exception that echoed the request payload back.

    Both flag directions, because they prove different things and neither
    implies the other:

      1. **Write path** — captured with the gate OFF, read back with it ON, so
         the read projection cannot be what is doing the hiding. The owner's two
         fields must have been *stored*; the sender-derived three must not.
      2. **Read path** — captured with the gate ON (all five stored), read with
         it OFF. The owner's two must still be served; the sender-derived three
         must not.
    """
    from app.services.routing import routing_trace

    channel = _channel(client, superuser_token_headers)
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)

    trigger_a = f"Handle calendar scheduling requests {random_lower_string()[:6]}"
    examples_a = "book a meeting\nschedule a call"
    trigger_b = f"Handle expense report questions {random_lower_string()[:6]}"
    examples_b = "file an expense\nwhere is my refund"

    agent_a = _agent_with_examples_route(
        client, headers, trigger_prompt=trigger_a, prompt_examples=examples_a, name_prefix="OwnerCfgA"
    )
    _agent_with_examples_route(
        client, headers, trigger_prompt=trigger_b, prompt_examples=examples_b, name_prefix="OwnerCfgB"
    )

    marker = f"SENDER-WORDS-{random_lower_string()}"

    def _post(store_text: bool) -> str:
        """Deliver one message; return the id of the row it produced.

        The new id is found by *diffing* the list against a snapshot taken
        first, not by indexing into it: the list orders by
        ``created_at DESC, id DESC``, and two deliveries in the same test can
        land close enough together that the tiebreak — a random UUID — decides
        which one comes back at ``[0]``. Picking the wrong row here would make
        the write-path half of this test silently assert against the read-path
        half's row.
        """
        before = {
            r["id"]
            for r in list_routing_traces(
                client, superuser_token_headers, channel_id=channel["id"]
            )["data"]
        }
        signer = GoogleChatJWTSigner()
        event = build_message_event(
            thread_key=f"spaces/AAA/threads/{random_lower_string()}",
            text=f"please book me a meeting {random_lower_string()}",
            sender_email=user["email"],
        )

        def _classify_and_note_attempt(*args, **kwargs):
            routing_trace.record_llm_attempt(
                provider="stub-provider",
                model="stub-model",
                ok=False,
                error=f"429 rate limited on request: {marker}",
                latency_ms=17,
            )
            return _classify_response(agent_a["id"])

        with patch(_SETTING, store_text):
            with patch(_PROVIDER_TARGET) as mock_pm:
                mock_pm.return_value.generate_content.side_effect = _classify_and_note_attempt
                resp, _ = post_channel_message(
                    client, channel, signer, event, classify_via_provider=True
                )
                assert mock_pm.return_value.generate_content.call_count >= 1
            assert resp.status_code == 200
        page = list_routing_traces(
            client, superuser_token_headers, channel_id=channel["id"]
        )
        new_ids = [r["id"] for r in page["data"] if r["id"] not in before]
        assert len(new_ids) == 1, page["data"]
        return new_ids[0]

    def _pass1(detail: dict) -> dict:
        return next(s for s in detail["stages"] if s["stage"] == "pass_1")

    def _candidate(stage: dict, agent_id: str) -> dict:
        return next(c for c in stage["candidates"] if c["ref_id"] == agent_id)

    # ── 0. Baseline: with the gate ON everything is stored AND served. ─────
    on_id = _post(store_text=True)
    with patch(_SETTING, True):
        detail_on = get_routing_trace(client, superuser_token_headers, on_id)
    stage_on = _pass1(detail_on)
    assert stage_on["prompt"], stage_on
    assert stage_on["raw_response"], stage_on
    assert marker in stage_on["llm_attempts"][0]["error"], stage_on
    assert _candidate(stage_on, agent_a["id"])["trigger_prompt"] == trigger_a
    assert _candidate(stage_on, agent_a["id"])["prompt_examples"] == examples_a

    # ── 1. Read path: the SAME row, read with the gate OFF. ───────────────
    with patch(_SETTING, False):
        detail_off = get_routing_trace(client, superuser_token_headers, on_id)
    stage_off = _pass1(detail_off)

    # The owner's own configuration survives — this is the widening under test.
    cand_a_off = _candidate(stage_off, agent_a["id"])
    assert cand_a_off["trigger_prompt"] == trigger_a, cand_a_off
    assert cand_a_off["prompt_examples"] == examples_a, cand_a_off
    # ...for every candidate, not just the winner: the near-miss diagnosis is
    # about the agents that LOST.
    assert any(
        c.get("trigger_prompt") == trigger_b and c.get("prompt_examples") == examples_b
        for c in stage_off["candidates"]
    ), stage_off["candidates"]

    # The sender-derived fields do not. `.get`, not `[...]`: a withheld field is
    # absent under an allowlist, not present-and-blanked.
    assert stage_off.get("prompt") is None, stage_off
    assert stage_off.get("raw_response") is None, stage_off
    assert stage_off["llm_attempts"][0].get("error") in (None, ""), stage_off
    assert marker not in json.dumps(detail_off), detail_off
    assert detail_off["message_text"] is None, detail_off
    # The outage diagnosis that needs no sender text is untouched.
    assert stage_off["llm_attempts"][0]["provider"] == "stub-provider"
    assert stage_off["llm_attempts"][0]["ok"] is False

    # ── 2. Write path: captured with the gate OFF, read back with it ON, so
    #      the read projection cannot be what hides anything. ───────────────
    off_id = _post(store_text=False)
    with patch(_SETTING, True):
        detail_written_off = get_routing_trace(client, superuser_token_headers, off_id)
    stage_written_off = _pass1(detail_written_off)

    cand_a_written = _candidate(stage_written_off, agent_a["id"])
    assert cand_a_written["trigger_prompt"] == trigger_a, cand_a_written
    assert cand_a_written["prompt_examples"] == examples_a, cand_a_written
    # Read gate wide open, so anything missing here was never written.
    assert stage_written_off.get("prompt") is None, stage_written_off
    assert stage_written_off.get("raw_response") is None, stage_written_off
    assert stage_written_off["llm_attempts"][0].get("error") in (None, ""), stage_written_off
    assert marker not in json.dumps(detail_written_off), detail_written_off
