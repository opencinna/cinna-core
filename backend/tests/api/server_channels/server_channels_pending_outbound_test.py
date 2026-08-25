"""Pending flow + outbound delivery — plan §13 checklist.

Covers:
  - Parking a message while an auto-installed environment builds.
  - `flush_pending_bindings` (called directly — see `tests/utils
    /server_channel.py::flush_pending_bindings` and the domain README for why)
    advancing a `pending_install` binding to `active` and delivering the
    parked message once the environment reports `running`.
  - The env-failure path: `status == "error"` fails the binding and notifies
    the thread, and does NOT advance it.
  - `STREAM_COMPLETED` outbound gating: a non-channel session's completion
    must never reach the channel adapter (the cheap
    `integration_type.startswith("channel_")` gate); a channel session's
    completion must deliver the final assistant message through the binding.

Chunking (`GoogleChatAdapter._chunk`) is pure text-splitting logic with no
I/O and is unit-tested instead, not here — see
`tests/unit/test_google_chat_adapter_chunk.py`.
"""
import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import CHANNEL_BINDING_ACTIVE, CHANNEL_BINDING_FAILED, ChannelThreadBinding
from app.services.sessions.channel_ingestion_service import ChannelDecline
from app.services.server_channels.channel_inbound_service import (
    _MAX_PARKED_MESSAGES,
    REPLY_SETUP_FAILED,
    REPLY_STILL_SETTING_UP,
    REPLY_TOO_MANY_QUEUED,
)
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.app_agent_route import create_user_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.environment import set_environment_status
from tests.utils.message import list_messages, send_message
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
    flush_pending_bindings,
    list_debug_events,
    post_webhook,
)
from tests.utils.routing import refuse_to_classify
from tests.utils.session import create_session_via_api, list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _setup_pending_install(client, superuser_headers) -> dict:
    """Consumer messages a channel, matches Pass 2, and parks — before the
    environment is ready. Returns everything the caller needs to advance it."""
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_headers, publisher["id"])
    agent = create_agent_via_api(client, publisher_headers, name=f"PendingBundle-{random_lower_string()[:6]}")
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle pending-flow test requests"},
    )
    assert r.status_code == 200, r.text
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()
    bundle_uuid = fresh["bundle_uuid"]

    channel = _channel(client, superuser_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_headers, bundle_uuid)

    classify_result = types.SimpleNamespace(agent_id=bundle_uuid, transformed_message=None)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(thread_key=thread_key, text="park me please", sender_email=consumer["email"])
    token = signer.token(audience=channel["config"]["project_number"])
    stub = StubAgentEnvConnector(response_text="Sure thing.")
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock, patch(_CLASSIFY_TARGET, return_value=classify_result):
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    assert resp.status_code == 200

    consumer_agents = client.get(f"{API}/agents/", headers=consumer_headers).json()["data"]
    installed = next(a for a in consumer_agents if a["bundle_uuid"] == bundle_uuid)

    return {
        "consumer_headers": consumer_headers,
        "installed_agent": installed,
        "env_id": installed["active_environment_id"],
        "stub": stub,
        "install_reply_texts": [c.args[-1] for c in send_mock.await_args_list],
        # For tests that keep posting into the same parked thread.
        "channel": channel,
        "signer": signer,
        "thread_key": thread_key,
        "consumer_email": consumer["email"],
    }


# ---------------------------------------------------------------------------
# Pending flow
# ---------------------------------------------------------------------------


def test_pending_flow_parks_then_flushes_once_env_is_running(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    setup = _setup_pending_install(client, superuser_token_headers)
    assert any("Setting up" in t for t in setup["install_reply_texts"])

    # Not yet delivered — the environment isn't running yet.
    assert list_sessions(client, setup["consumer_headers"]) == []

    set_environment_status(db, setup["env_id"], "running")
    db.commit()

    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock:
        advanced = flush_pending_bindings(db)
        drain_tasks()

    assert advanced == 1
    ready_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any("ready" in t.lower() for t in ready_texts)

    sessions = [
        s for s in list_sessions(client, setup["consumer_headers"])
        if s["agent_id"] == setup["installed_agent"]["id"]
    ]
    assert len(sessions) == 1
    user_msgs = [m for m in list_messages(client, setup["consumer_headers"], sessions[0]["id"]) if m["role"] == "user"]
    assert any("park me please" in (m["content"] or "") for m in user_msgs)

    # A second flush is a no-op — the binding already advanced.
    with patch(_STREAM_TARGET, setup["stub"]):
        advanced_again = flush_pending_bindings(db)
    assert advanced_again == 0


def test_pending_flow_env_error_fails_the_binding_without_delivering(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    setup = _setup_pending_install(client, superuser_token_headers)

    set_environment_status(db, setup["env_id"], "error")
    db.commit()

    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock:
        advanced = flush_pending_bindings(db)
        drain_tasks()

    assert advanced == 0
    failure_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert any("failed" in t.lower() or "administrator" in t.lower() for t in failure_texts)

    # The parked message was never delivered — no session exists.
    assert list_sessions(client, setup["consumer_headers"]) == []


# ---------------------------------------------------------------------------
# Outbound: STREAM_COMPLETED gating + binding lookup
# ---------------------------------------------------------------------------


def test_stream_completed_never_reaches_the_adapter_for_a_non_channel_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The cheap integration_type gate: an ordinary session's STREAM_COMPLETED
    must not touch the channel adapter at all."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"PlainSession-{random_lower_string()[:6]}")
    drain_tasks()
    session = create_session_via_api(client, headers, agent["id"])

    stub = StubAgentEnvConnector(response_text="A perfectly ordinary reply.")
    with patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock:
        send_message(client, headers, session["id"], "hello agent")
        drain_tasks()

    send_mock.assert_not_awaited()


def test_stream_completed_delivers_final_message_through_the_binding(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A channel session's STREAM_COMPLETED must resolve the binding and
    deliver the final assistant message via the adapter, into the right
    thread_key."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"OutboundGate-{random_lower_string()[:6]}")
    drain_tasks()
    # Channel Pass 1 routes over the sender's own agents: the agent's own
    # trigger prompt is what makes it a candidate. One eligible agent and an
    # empty auto-install list is Pass 1's `only_one` short-circuit, so the
    # classifier below is stubbed to RAISE — reaching it would mean the
    # short-circuit stopped firing, and this test would rather say so than
    # quietly call a model.
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    reply_text = "Here is the final assistant answer."
    event = build_message_event(thread_key=thread_key, text="hello", sender_email=user["email"])
    token = signer.token(audience=channel["config"]["project_number"])
    stub = StubAgentEnvConnector(response_text=reply_text)
    with signer.patched(), patch(_STREAM_TARGET, stub), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ) as send_mock, patch(_CLASSIFY_TARGET, refuse_to_classify):
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()

    assert resp.status_code == 200
    delivered_calls = [c.args for c in send_mock.await_args_list]
    # (channel, thread_key, text) — the last positional arg is the text.
    assert any(reply_text in (args[-1] or "") for args in delivered_calls)
    assert any(args[-2] == thread_key for args in delivered_calls)


def test_parked_cap_refuses_and_a_refused_message_is_not_deduped_away(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The parking cap refuses honestly, and a refused message stays recoverable.

    Two coupled properties, which is why they are asserted together:

      1. Once `_MAX_PARKED_MESSAGES` are queued, further messages get
         `REPLY_TOO_MANY_QUEUED` — telling the truth rather than promising
         "I'll answer shortly" about a message that was dropped.
      2. A refused message is NOT stamped as delivered, so redelivering it
         reaches the pipeline again instead of being silently acked. That
         only holds while the in-process `_seen_recently` guard is gated on
         `binding is None`: it records its key at webhook time, before the
         cap is even consulted, so running it for an existing binding would
         ack the redelivery with `{}` and quietly discard the message the
         cap had just refused.
    """
    setup = _setup_pending_install(client, superuser_token_headers)
    channel, signer = setup["channel"], setup["signer"]
    thread_key, email = setup["thread_key"], setup["consumer_email"]
    token = signer.token(audience=channel["config"]["project_number"])

    def _send(text: str, message_name: str):
        event = build_message_event(
            thread_key=thread_key,
            text=text,
            sender_email=email,
            message_name=message_name,
        )
        with signer.patched(), patch(_SEND_TARGET, AsyncMock(return_value="x")):
            resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
            drain_tasks()
        return resp

    # Setup already parked one message; fill the queue to exactly the cap.
    for i in range(_MAX_PARKED_MESSAGES - 1):
        r = _send(f"filler {i}", f"spaces/AAA/messages/fill-{i}")
        assert r.status_code == 200
        assert r.json().get("text") == REPLY_STILL_SETTING_UP, (
            f"message {i} should have been accepted into the queue"
        )

    # One past the cap: refused, and told so.
    refused_id = "spaces/AAA/messages/over-the-cap"
    r_refused = _send("this one overflows", refused_id)
    assert r_refused.status_code == 200
    refused_text = r_refused.json().get("text", "")
    assert refused_text == REPLY_TOO_MANY_QUEUED, (
        f"Cap-refused message got an acceptance reply: {refused_text!r}"
    )

    # Redelivery of that same refused message must reach the pipeline again —
    # same honest refusal, never a silent `{}` ack.
    r_again = _send("this one overflows", refused_id)
    assert r_again.status_code == 200
    assert r_again.json() != {}, (
        "Redelivery of a cap-refused message was silently acked — it was "
        "deduped away despite never having been accepted."
    )
    assert r_again.json().get("text", "") == refused_text


def test_post_drain_repark_at_the_cap_tells_the_sender_instead_of_dropping(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Regression: a message could be dropped in silence after a failed drain.

    `_flush_one` commits `active` BEFORE draining, so a drain that fails leaves
    an ACTIVE binding still holding its parked queue. The next inbound message
    then re-parks at the back of that queue rather than overtaking it — and
    when the queue is already at `_MAX_PARKED_MESSAGES`, `_append_parked`
    refuses. That refusal used to be discarded: the message was gone and the
    sender was told nothing, left waiting for an answer that could never come.

    `_park_message` has held the opposite invariant all along ("never drop in
    silence"), and this path must match it. The reply travels out-of-band via
    the adapter, not in the webhook response, because an ACTIVE binding is
    acked silently — which is precisely why a dropped message here was
    invisible.

    The drain is failed by making the ingest call raise, with the environment
    left `running`. Failing the environment instead would take the binding
    down a different path (`_flush_one` fails it outright) and never reach the
    re-park at all.
    """
    ingest_target = (
        "app.services.sessions.channel_ingestion_service"
        ".ChannelIngestionService.ingest_inbound_message"
    )

    setup = _setup_pending_install(client, superuser_token_headers)
    channel, signer = setup["channel"], setup["signer"]
    thread_key, email = setup["thread_key"], setup["consumer_email"]
    token = signer.token(audience=channel["config"]["project_number"])

    def _send(text: str, message_name: str, *, fail_ingest: bool):
        event = build_message_event(
            thread_key=thread_key,
            text=text,
            sender_email=email,
            message_name=message_name,
        )
        stack = [
            patch(_STREAM_TARGET, setup["stub"]),
            patch(_SEND_TARGET, AsyncMock(return_value="x")),
            signer.patched(),
        ]
        if fail_ingest:
            stack.append(
                patch(ingest_target, AsyncMock(side_effect=RuntimeError("boom")))
            )
        with ExitStack() as es:
            for ctx in stack:
                es.enter_context(ctx)
            send_mock = stack[1].new
            resp = post_webhook(
                client, channel["webhook_token"], event, bearer_token=token
            )
            drain_tasks()
        return resp, send_mock

    # Fill the parked queue to exactly the cap (setup parked one already).
    for i in range(_MAX_PARKED_MESSAGES - 1):
        r, _ = _send(f"filler {i}", f"spaces/AAA/messages/f-{i}", fail_ingest=False)
        assert r.status_code == 200

    # Environment comes up, but every ingest fails: the flush flips the binding
    # to ACTIVE, then the drain dies on the first message and leaves all of
    # them parked. That is the state the bug needs.
    set_environment_status(db, setup["env_id"], "running")
    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="x")
    ), patch(ingest_target, AsyncMock(side_effect=RuntimeError("boom"))):
        flush_pending_bindings(db)
        drain_tasks()

    binding = db.exec(
        select(ChannelThreadBinding).where(
            ChannelThreadBinding.thread_key == thread_key
        )
    ).first()
    db.refresh(binding)
    assert binding.status == CHANNEL_BINDING_ACTIVE
    assert len(binding.pending_messages or []) == _MAX_PARKED_MESSAGES, (
        "precondition: the failed drain must leave the queue at the cap"
    )

    # One more message: the drain fails again, the queue is still at the cap,
    # so the re-park is refused. The sender must be told.
    _, send_mock = _send(
        "this one overflows", "spaces/AAA/messages/overflow", fail_ingest=True
    )

    sent_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert REPLY_TOO_MANY_QUEUED in sent_texts, (
        "A cap-refused message after a failed drain was dropped in silence. "
        f"Texts actually delivered: {sent_texts!r}"
    )


# ---------------------------------------------------------------------------
# Deterministic vs transient failures while draining (channels/identity
# unification, phase 4)
# ---------------------------------------------------------------------------

_INGEST_TARGET = (
    "app.services.sessions.channel_ingestion_service"
    ".ChannelIngestionService.ingest_inbound_message"
)


def _binding_by_thread_key(db: Session, thread_key: str) -> ChannelThreadBinding | None:
    return db.exec(
        select(ChannelThreadBinding).where(ChannelThreadBinding.thread_key == thread_key)
    ).first()


def test_deterministic_decline_while_draining_fails_binding_drops_queue_and_self_heals(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    `_drain_parked` classifies a `ChannelDecline` from `_ingest` as a
    DETERMINISTIC decline — one that will recur identically on every retry
    (the sender's `allow_identity_routing` consent off, a revoked identity
    grant, or the `user.id != binding.user_id` invariant guard) — and fails
    the binding instead of leaving it parked forever, which used to wedge the
    thread until the parked-message cap.

      1. A `pending_install` binding with one parked message; the environment
         comes up and `_ingest` declines deterministically.
      2. The binding ends `failed` — not left `pending_install`/`active` with
         the message still queued — which is what arms the self-heal.
      3. The parked queue is dropped, and the debug feed's structured detail
         records that the decline was deterministic and how many messages
         were dropped. Asserted on those facts (deterministic-ness, the
         count), not on exact prose.
      4. The sender gets the SAME generic setup-failed notice every other
         failure gets — no oracle for which gate closed.
      5. The next message on that thread deletes the failed binding and
         re-routes from scratch: the thread is not wedged.
    """
    setup = _setup_pending_install(client, superuser_token_headers)
    channel, signer = setup["channel"], setup["signer"]
    thread_key, email = setup["thread_key"], setup["consumer_email"]

    set_environment_status(db, setup["env_id"], "running")
    db.commit()

    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="x")
    ) as send_mock, patch(
        _INGEST_TARGET,
        AsyncMock(
            side_effect=ChannelDecline(
                "identity routing is switched off for this sender on this channel"
            )
        ),
    ):
        advanced = flush_pending_bindings(db)
        drain_tasks()

    # `_flush_one` flips the binding to `active` and commits BEFORE draining,
    # so it still counts as "advanced" even though the drain then fails it.
    assert advanced == 1

    binding = _binding_by_thread_key(db, thread_key)
    assert binding is not None
    db.refresh(binding)
    failed_binding_id = binding.id
    assert binding.status == CHANNEL_BINDING_FAILED
    assert not binding.pending_messages, binding.pending_messages
    assert "deterministic" in (binding.last_error or "").lower()

    feed = list_debug_events(client, superuser_token_headers, channel["id"])
    parked_drain_events = [
        e for e in feed["events"] if (e.get("detail") or {}).get("stage") == "parked_drain"
    ]
    assert len(parked_drain_events) == 1, feed["events"]
    detail = parked_drain_events[0]["detail"]
    assert detail["failure_class"] == "deterministic"
    assert detail["dropped_parked_messages"] == "1"

    sent_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert REPLY_SETUP_FAILED in sent_texts, sent_texts

    # ── Self-heal: the next message deletes the failed binding and re-routes ──
    event2 = build_message_event(
        thread_key=thread_key, text="are you still there?", sender_email=email
    )
    token2 = signer.token(audience=channel["config"]["project_number"])
    stub2 = StubAgentEnvConnector(response_text="back online")
    with signer.patched(), patch(_STREAM_TARGET, stub2), patch(
        _SEND_TARGET, AsyncMock(return_value="x")
    ), patch(_CLASSIFY_TARGET, refuse_to_classify):
        resp2 = post_webhook(client, channel["webhook_token"], event2, bearer_token=token2)
        drain_tasks()
    assert resp2.status_code == 200

    sessions = [
        s for s in list_sessions(client, setup["consumer_headers"])
        if s["agent_id"] == setup["installed_agent"]["id"]
    ]
    assert len(sessions) == 1, sessions
    user_msgs = [
        m for m in list_messages(client, setup["consumer_headers"], sessions[0]["id"])
        if m["role"] == "user"
    ]
    assert any("are you still there?" in (m["content"] or "") for m in user_msgs)

    healed_binding = _binding_by_thread_key(db, thread_key)
    assert healed_binding is not None
    # A genuinely different row: the failed one was deleted, not repaired.
    assert healed_binding.id != failed_binding_id
    assert healed_binding.status == CHANNEL_BINDING_ACTIVE


def test_transient_failure_while_draining_leaves_binding_active_with_messages_still_parked(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The arm that must not regress. Anything other than a `ChannelDecline`
    from `_ingest` during the parked-queue drain is a TRANSIENT failure — a
    later attempt (the next inbound message, or the next scheduler tick) might
    succeed — so the binding must NOT be failed and the parked messages must
    NOT be dropped. This is the whole reason the two arms exist: before this
    change every failure here took this branch, including the deterministic
    ones, which retried forever and wedged the thread.
    """
    setup = _setup_pending_install(client, superuser_token_headers)
    thread_key = setup["thread_key"]

    set_environment_status(db, setup["env_id"], "running")
    db.commit()

    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="x")
    ) as send_mock, patch(
        _INGEST_TARGET, AsyncMock(side_effect=RuntimeError("transient boom"))
    ):
        flush_pending_bindings(db)
        drain_tasks()

    binding = _binding_by_thread_key(db, thread_key)
    assert binding is not None
    db.refresh(binding)
    assert binding.status == CHANNEL_BINDING_ACTIVE  # NOT failed
    assert len(binding.pending_messages or []) == 1, binding.pending_messages
    assert "deterministic" not in (binding.last_error or "").lower()

    # Same generic notice as the deterministic arm — indistinguishable to the
    # sender, only the binding's fate differs.
    sent_texts = [c.args[-1] for c in send_mock.await_args_list]
    assert REPLY_SETUP_FAILED in sent_texts, sent_texts
