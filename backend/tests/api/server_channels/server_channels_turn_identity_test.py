"""Turn identity — a turn's reply is the message *that turn wrote*, never the
newest agent row in the session.

The bug this file is the reproducer for shipped, and it was not theoretical:
``ChannelOutboundService.handle_stream_completed`` resolved its text with
``_last_agent_message`` — "the newest ``role='agent'`` row in this session by
sequence number" — which answers a question about the *session*, not about the
*turn*. A turn that writes no agent row at all therefore re-delivered the
**previous** turn's answer into the thread, as if it were the reply to the new
message. Two shapes reach that state today:

* a **command turn** (``/run:<name>``) — the command stream writes one
  ``role="system"`` message and never an agent row;
* a **batch with no storable events** — ``streaming_events`` stays empty, the
  finalize block is skipped, and no row is created.

(A *tool-only* batch is **not** one of them, despite being the obvious guess:
``message_service`` gives such a batch the literal ``"Agent response"``
placeholder, so it does write a row. Channel delivery no longer sends that
placeholder — the uuid arm detects the tool-only shape from the row's stored
events and delivers a compact tool summary instead; see
``channel_tool_summary.py`` and ``tests/unit/test_channel_tool_summary.py``.)

The fix is turn identity in the terminal stream events'
meta — ``agent_message_id``, the row the batch wrote or an explicit ``None`` —
and consumers that deliver exactly that row. Layered on top is the
``channel_turn_delivery`` ledger: one durable row per external message a turn
put on screen, which makes a duplicate completion idempotent and turns the
relay-versus-finalized-text assumption into a check that can fire.

What is asserted here, and where the rest lives:

* Everything a **reader** sees goes through the four Google Chat verbs, the
  ``_Chat`` shape this domain shares (see the README's "Patterns specific to
  this domain": a test that wants real notice behaviour must mock all four,
  because a mocked ``send_message`` cannot return a usable id and every state
  would degrade to a fresh post).
* The **ledger's own** contract — which rows exist, what they are attributed
  to, which one is ``final``, which one is ``diverged`` — is invisible from
  the thread by construction (the divergence check is deliberately
  observational and delivers nothing either way), so it is read straight off
  the table through ``tests/utils/server_channel.py::list_turn_deliveries``,
  a read-only Rule-1 exemption in the same narrow spirit as
  ``get_binding_status_message_id``.
* The emitter's own meta and the consumer's branch matrix are pure logic and
  are unit-tested next door: ``tests/unit/test_stream_turn_identity_meta.py``
  and ``tests/unit/test_channel_turn_identity_consumer.py``. The ledger's
  totality (a failed write never raises into a delivery) is
  ``tests/unit/test_channel_turn_delivery_ledger.py``; the relay's own
  boundary bookkeeping is ``tests/unit/test_channel_stream_relay.py``.

``handle_stream_error`` / ``handle_stream_interrupted`` gained **no** consumer
behaviour change in this feature and must not acquire one — their branches are
the previous feature's shipped regression guards. What they gained is a ledger
close-out in a ``finally``, and the interrupted scenarios below exist to prove
that close-out changed none of the five branches it now sits under.
"""
import uuid
from contextlib import ExitStack
from itertools import count
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.email.outgoing_email_queue import OutgoingEmailQueue
from app.services.server_channels.adapters.base import (
    ChannelReplaceResult,
    ChannelSendError,
)
from app.services.server_channels.channel_inbound_service import (
    REPLY_WORKING,
    REPLY_WORKING_ON_IT,
)
from app.services.server_channels.channel_outbound_service import (
    STOPPED_NOTICE,
    STOPPED_SUFFIX,
)
from tests.stubs.agent_env_stub import (
    StubAgentEnvConnector,
    build_command_stream_events,
)
from tests.stubs.email_stubs import StubIMAPConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.email_channel import (
    IMAP_CONNECTOR_TARGET,
    build_raw_email,
    create_email_channel,
    poll_channel,
)
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.message import list_messages
from tests.utils.routing import refuse_to_classify
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    get_binding_status_message_id,
    list_turn_deliveries,
    post_webhook,
    replay_stream_completed,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_ADAPTER = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"

#: A service-account blob so the channel reads as having an outbound
#: credential — same reason as the sibling streaming files: a channel that
#: cannot post has no notice to roll and every assertion here would be about
#: the wrong thing.
_SECRETS = '{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "x"}'

#: Consumed rather than yielded by ``_LiveStream``: "let the event loop run
#: here". Same marker and the same reason as
#: ``server_channels_streaming_updates_test.py`` — the relay's flusher is a
#: sibling asyncio task and ``StubAgentEnvConnector`` yields its canned events
#: with no ``await``, so without this the flusher never wakes and every
#: relay-dependent assertion below would pass by measuring a turn that never
#: streamed.
_PAUSE = {"type": "__pause__"}

_CLI_COMMANDS_YAML = (
    b"commands:\n"
    b"  - name: check\n"
    b"    command: uv run /app/workspace/scripts/check.py\n"
    b"    description: Run the check script\n"
)


class _LiveStream(StubAgentEnvConnector):
    """``StubAgentEnvConnector`` that hands the event loop a turn at ``_PAUSE``."""

    async def stream_chat(self, base_url, auth_headers, payload):
        import asyncio

        self.stream_calls.append({"base_url": base_url, "payload": payload})
        for event in self.events:
            if event is _PAUSE:
                await asyncio.sleep(0)
                continue
            yield event


class _Chat:
    """The four outbound verbs, mocked together, with serial message ids.

    The shape ``server_channels_status_notice_test.py`` established and the
    streaming/stop files reuse: all four verbs, a real-looking
    ``spaces/AAA/messages/…`` id out of ``send_message`` (anything else is
    refused by ``GoogleChatAdapter._message_url`` and degrades every state to
    a fresh post), and a ``ChannelReplaceResult`` out of ``replace_message``
    because ``_deliver`` reads ``.replaced`` to decide whether the notice was
    really taken over.
    """

    def __init__(self) -> None:
        self._ids = count(1)
        self.send = AsyncMock(side_effect=self._next_id)
        self.update = AsyncMock(return_value=None)
        self.replace = AsyncMock(side_effect=self._replaced)
        self.delete = AsyncMock(return_value=None)

    def _next_id(self, *_args, **_kwargs) -> str:
        return f"spaces/AAA/messages/m{next(self._ids)}"

    @staticmethod
    def _replaced(_channel, _thread_key, message_id, _text) -> ChannelReplaceResult:
        return ChannelReplaceResult(message_id=message_id, replaced=True)

    def apply(self, stack: ExitStack) -> "_Chat":
        stack.enter_context(patch(f"{_ADAPTER}.send_message", self.send))
        stack.enter_context(patch(f"{_ADAPTER}.update_message", self.update))
        stack.enter_context(patch(f"{_ADAPTER}.replace_message", self.replace))
        stack.enter_context(patch(f"{_ADAPTER}.delete_message", self.delete))
        return self

    @property
    def sent(self) -> list[str]:
        return [c.args[-1] or "" for c in self.send.await_args_list]

    @property
    def updated(self) -> list[tuple[str, str]]:
        return [(c.args[2], c.args[3] or "") for c in self.update.await_args_list]

    @property
    def replaced(self) -> list[tuple[str, str]]:
        return [(c.args[2], c.args[3] or "") for c in self.replace.await_args_list]

    @property
    def deleted(self) -> list[str]:
        return [c.args[-1] for c in self.delete.await_args_list]

    @property
    def outbound_text(self) -> str:
        """Every byte this thread was shown, by any verb."""
        return "\n".join(
            self.sent + [t for _, t in self.updated] + [t for _, t in self.replaced]
        )


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*", secrets=_SECRETS)
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _sender_with_one_agent(client, superuser_headers, *, label="Turn"):
    """A sender who owns exactly one eligible agent (Pass 1's ``only_one``).

    With an empty auto-install list this routes with no LLM at all, which is
    why the classifier stub below is ``refuse_to_classify``: naming no answer
    is the stronger form, because the stub raises if classification is
    reached after all.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _post(client, channel, signer, event, chat: _Chat, stub, **overrides):
    """One verified webhook delivery, drained, with the four verbs observed."""
    settings_overrides = {"CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS": 0}
    settings_overrides.update(overrides)
    token = signer.token(audience=channel["config"]["project_number"])
    with ExitStack() as stack:
        for name, value in settings_overrides.items():
            stack.enter_context(patch.object(settings, name, value))
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, refuse_to_classify))
        chat.apply(stack)
        resp = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        drain_tasks()
    return resp


def _channel_session_id(client, headers, agent_id) -> str:
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent_id]
    assert len(sessions) == 1, sessions
    return sessions[0]["id"]


def _agent_message_ids(client, headers, session_id) -> list[str]:
    """The session's agent rows, oldest first — the ids turn identity names."""
    return [
        m["id"]
        for m in list_messages(client, headers, session_id)
        if m["role"] == "agent"
    ]


def _rows(db, channel, thread_key) -> list[tuple[str, int, str]]:
    """``(role, part_index, status)`` for every ledger row on one thread."""
    return [
        (r.role, r.part_index, r.status)
        for r in list_turn_deliveries(db, channel["id"], thread_key)
    ]


# ---------------------------------------------------------------------------
# The headline — the stale-turn bug, end to end
# ---------------------------------------------------------------------------


def test_a_command_turn_never_redelivers_the_previous_turns_answer(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """The bug's reproducer: a ``/run:*`` turn must not repeat the last answer.

    1. Turn one is an ordinary question and the thread gets answer A.
    2. Turn two on the same thread is ``/run:check``. A command stream writes
       one ``role="system"`` message and **never** an agent row, so the only
       ``role="agent"`` row in this session is still turn one's — and that is
       precisely what ``_last_agent_message`` used to hand back.
    3. The thread must not be shown answer A a second time. The command turn
       has nothing to say through this surface, so its notice is **cleared**
       and the binding's id released.

    This test fails on pre-Phase-A code. Verified by mutation rather than
    asserted on faith: dropping ``agent_message_id=None`` from the
    command-stream emission in ``message_service`` (which is exactly what an
    event from before this feature looks like — no key at all, so the
    consumer's legacy arm re-runs the newest-row query) makes the run below
    deliver answer A into turn two's notice and this test go red.

    The command really ran, and that is asserted rather than assumed: the
    absence of answer A would be equally true of a turn that never happened.
    """
    shared_adapter = EnvironmentTestAdapter()
    shared_adapter.workspace_files = {"docs/CLI_COMMANDS.yaml": _CLI_COMMANDS_YAML}
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    # ── Phase 1: turn one answers the question ────────────────────────────
    answer_a = "Answer A: the deployment finished at 14:02."
    chat1 = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="what happened?", sender_email=user["email"]
        ),
        chat1,
        StubAgentEnvConnector(response_text=answer_a),
    )
    assert resp.status_code == 200
    assert chat1.replaced == [("spaces/AAA/messages/m1", answer_a)], chat1.replaced

    session_id = _channel_session_id(client, headers, agent["id"])
    agent_rows_after_turn_one = _agent_message_ids(client, headers, session_id)
    assert len(agent_rows_after_turn_one) == 1, agent_rows_after_turn_one

    # Turn one settled itself in the ledger: one ``final`` row, nothing else.
    assert _rows(db, channel, thread_key) == [("final", 0, "delivered")]

    # ── Phase 2: teach the environment a CLI command ──────────────────────
    r = client.get(f"{API}/sessions/{session_id}/commands", headers=headers)
    assert r.status_code == 200, r.text
    drain_tasks()
    assert "/run:check" in [c["name"] for c in r.json()["commands"]], r.json()

    # ── Phase 3: turn two is a command turn ───────────────────────────────
    cmd_stub = StubAgentEnvConnector(
        response_text="never used — a command turn takes the command path",
        command_events=build_command_stream_events(
            exec_id=str(uuid.uuid4()),
            command="uv run /app/workspace/scripts/check.py",
            stdout_lines=["All checks passed.\n"],
            exit_code=0,
        ),
    )
    chat2 = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="/run:check", sender_email=user["email"]
        ),
        chat2,
        cmd_stub,
    )
    assert resp.status_code == 200

    # The command genuinely executed — so "answer A is absent" is a statement
    # about attribution and not about a turn that quietly did nothing.
    assert len(cmd_stub.stream_command_calls) == 1, cmd_stub.stream_command_calls
    assert cmd_stub.stream_calls == [], "a command turn must not reach the LLM path"
    assert len(_agent_message_ids(client, headers, session_id)) == 1, (
        "a command stream writes a system message, never an agent row"
    )

    # ── Phase 4: the thread was NOT told answer A again ───────────────────
    assert answer_a not in chat2.outbound_text, chat2.outbound_text
    # Its own notice — a bound thread skips routing, so the notice opens
    # straight on the working state — and then nothing to put in it.
    assert chat2.sent == [REPLY_WORKING_ON_IT], chat2.sent
    assert chat2.replaced == [], chat2.replaced
    # The one place a notice really is deleted: the turn is over and there is
    # nothing to leave in its slot, so it must not survive to be rewritten by
    # the next turn as if we were still working on this message.
    assert chat2.deleted == ["spaces/AAA/messages/m1"], chat2.deleted
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None

    # ── Phase 5: and the ledger recorded nothing for it ───────────────────
    # An event that names no agent message attributes nothing and settles
    # nothing — in particular it does not adopt turn one's row, which is
    # already attributed and must stay that way.
    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert [(r.role, r.part_index, r.status) for r in rows] == [
        ("final", 0, "delivered")
    ]
    assert str(rows[0].session_message_id) == agent_rows_after_turn_one[0]


# ---------------------------------------------------------------------------
# The same bug, email-shaped
# ---------------------------------------------------------------------------


def test_an_empty_turn_on_email_sends_no_mail_carrying_the_previous_answer(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Email is the pure case: it never attaches a relay, so every reply there
    resolves through the arm this feature changed.

    ``maybe_attach_channel_relay`` gates on the transport declaring
    ``supports_status_notice``, and email declares none — there is no message
    to rewrite. So an email turn's completion always takes the full-text path,
    which is where ``_last_agent_message`` used to sit. A batch that produces
    **no storable events at all** writes no agent row (the finalize block is
    guarded by ``if streaming_events:``), and before turn identity that turn
    mailed the previous answer out a second time under the new subject.

    Two mails in, exactly one reply out.
    """
    imap_id = create_imap_server(client, superuser_token_headers)["id"]
    smtp_id = create_smtp_server(client, superuser_token_headers)["id"]
    mailbox = "support@corp.example"
    channel = create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Mail"
    )

    # ── Phase 1: the first mail is answered ───────────────────────────────
    answer_a = "Answer A: your order shipped on Tuesday."
    root_id = f"<{random_lower_string()}@sender.example>"
    first = build_raw_email(
        message_id=root_id,
        sender=user["email"],
        to=mailbox,
        subject="Where is my order",
        body="any news?",
    )
    with (
        patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[first])),
        patch(_STREAM_TARGET, StubAgentEnvConnector(response_text=answer_a)),
        patch(_CLASSIFY_TARGET, refuse_to_classify),
    ):
        assert poll_channel(db) == 1
        drain_tasks()

    session_id = _channel_session_id(client, headers, agent["id"])
    queued = _outgoing_bodies(db, session_id)
    assert len(queued) == 1, queued
    assert answer_a in queued[0]

    # ── Phase 2: the reply's turn produces nothing storable ───────────────
    # ``session_created`` and ``done`` are both excluded from
    # ``streaming_events``, so this batch writes no agent row at all — the
    # exact state in which the newest-row query answered with turn one's text.
    empty_stream = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "done"},
    ]
    reply_id = f"<{random_lower_string()}@sender.example>"
    second = build_raw_email(
        message_id=reply_id,
        sender=user["email"],
        to=mailbox,
        subject="Re: Where is my order",
        body="thanks — and one more thing",
        in_reply_to=root_id,
        references=root_id,
    )
    with (
        patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[second])),
        patch(_STREAM_TARGET, StubAgentEnvConnector(events=empty_stream)),
        patch(_CLASSIFY_TARGET, refuse_to_classify),
    ):
        assert poll_channel(db) == 1
        drain_tasks()

    # ── Phase 3: the second turn really ran, on the same thread ───────────
    # Both mails landed in one session (so this is the same thread the first
    # answer went to), and the second one reached the agent.
    assert _channel_session_id(client, headers, agent["id"]) == session_id
    user_msgs = [
        m for m in list_messages(client, headers, session_id) if m["role"] == "user"
    ]
    assert len(user_msgs) == 2, [m["content"] for m in user_msgs]
    assert "one more thing" in (user_msgs[1]["content"] or "")

    # ── Phase 4: and no second mail went out ──────────────────────────────
    after = _outgoing_bodies(db, session_id)
    assert len(after) == 1, after
    assert after == queued, "the empty turn must queue no mail at all"
    assert sum(body.count(answer_a) for body in after) == 1


def _outgoing_bodies(db: Session, session_id: str) -> list[str]:
    """Every queued outbound mail body for a session, oldest first.

    ``OutgoingEmailQueue`` has no read API — the row *is* the observable
    effect, exactly as ``server_channels_email_test.py`` already treats it.
    """
    db.expire_all()
    return [
        row.body or ""
        for row in db.exec(
            select(OutgoingEmailQueue)
            .where(OutgoingEmailQueue.session_id == uuid.UUID(session_id))
            .order_by(OutgoingEmailQueue.created_at)
        ).all()
    ]


# ---------------------------------------------------------------------------
# The interrupted path — unchanged, and proven unchanged
# ---------------------------------------------------------------------------


def test_a_turn_stopped_before_the_agent_spoke_still_says_so(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Regression guard for the previous feature's fix, re-run under the ledger.

    ``handle_stream_interrupted`` acquired a ledger close-out in a ``finally``
    under all five of its branches. A ``finally`` that neither raises nor
    returns cannot change which branch ran — but that is an argument, and this
    is the measurement: the likeliest stop there is (a turn cancelled before
    the agent produced a single token) must still turn its spinner into the
    visible "⏹️ Stopped." acknowledgement, in the same message, with the id
    released and nothing deleted.

    The close-out is a no-op here for a reason worth pinning: with no assistant
    event there is no agent row, so the event names no turn, so there is
    nothing to attribute — and the handler must not invent a key to put a row
    under. The ledger stays empty.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Stop"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]
    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="actually never mind",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
    )

    assert resp.status_code == 200
    notice_id = "spaces/AAA/messages/m1"
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert chat.updated == [
        (notice_id, REPLY_WORKING_ON_IT),
        (notice_id, STOPPED_NOTICE),
    ], chat.updated
    assert chat.replaced == [] and chat.deleted == []
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None

    # Nothing was delivered as an answer, so nothing is recorded as one.
    assert _rows(db, channel, thread_key) == []


def test_an_interrupted_turn_hands_no_rows_to_the_next_turn(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The close-out: a terminated turn's rows are attributed to *it*.

    ``settle_turn``'s adoption is deliberately greedy — it takes every pending
    row on the binding — because a boundary write cannot know its turn's
    identity yet. So a turn that seals text into the thread and is then
    **stopped** never reaches a completion, and without a close-out its rows
    would stay pending and be swept up by the *next* completion on the thread:
    the ledger would record one turn's messages as part of another, and the
    divergence check would then compare a new answer against an old prefix and
    report a mismatch that never happened.

    Three things are asserted, and the third is the sharp one:

    1. the interrupted turn's rows carry **its own** agent message id;
    2. it wrote **no ``final`` row** — nothing was delivered as an answer;
    3. the next turn on the thread still gets its reply. A close-out row can
       never satisfy ``turn_already_settled`` (whose gate matches on
       ``role == "final"``), so it can never be the reason a later legitimate
       completion withholds a delivery.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Cut"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    filler = "word " * 14
    para_1 = f"Paragraph one. {filler}"
    para_2 = f"Paragraph two. {filler}"
    stopped_stream = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": f"{para_1}\n\n", "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": para_2, "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]

    # ── Phase 1: a turn that seals, then is stopped ───────────────────────
    chat1 = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="write me a long answer",
            sender_email=user["email"],
        ),
        chat1,
        _LiveStream(events=stopped_stream),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
    )
    assert resp.status_code == 200
    # The seal really happened — the sealed slice is standing as its own
    # message and the stop marker is under it, not over it.
    assert para_1 in chat1.outbound_text, chat1.outbound_text
    assert STOPPED_SUFFIX in chat1.outbound_text or STOPPED_NOTICE in chat1.outbound_text

    session_id = _channel_session_id(client, headers, agent["id"])
    stopped_message_id = _agent_message_ids(client, headers, session_id)[-1]

    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert rows, "the sealing relay should have written boundary rows"
    # 1 + 2: attributed to the interrupted turn, and none of them final.
    assert {str(r.session_message_id) for r in rows} == {stopped_message_id}, rows
    assert [r.role for r in rows if r.role == "final"] == [], [r.role for r in rows]
    closed_out = len(rows)

    # ── Phase 2: the next turn is answered normally ───────────────────────
    answer_b = "Answer B: here is the short version."
    chat2 = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="shorter please", sender_email=user["email"]
        ),
        chat2,
        StubAgentEnvConnector(response_text=answer_b),
    )
    assert resp.status_code == 200
    # 3: the reply landed. A close-out that had counted as a settlement would
    # have made this turn skip its own delivery entirely.
    assert [t for _, t in chat2.replaced] == [answer_b], chat2.replaced

    new_message_id = _agent_message_ids(client, headers, session_id)[-1]
    assert new_message_id != stopped_message_id

    after = list_turn_deliveries(db, channel["id"], thread_key)
    assert len(after) == closed_out + 1, [(r.role, r.part_index) for r in after]
    # The stopped turn's rows kept their own attribution; the new turn owns
    # exactly one row, its own ``final``.
    by_message: dict[str, list[str]] = {}
    for row in after:
        by_message.setdefault(str(row.session_message_id), []).append(row.role)
    assert by_message[new_message_id] == ["final"], by_message
    assert "final" not in by_message[stopped_message_id], by_message


# ---------------------------------------------------------------------------
# Ledger idempotency
# ---------------------------------------------------------------------------


def test_a_duplicate_completion_delivers_the_answer_once(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A redelivered ``STREAM_COMPLETED`` must not post the answer twice.

    ``STREAM_COMPLETED`` is a bus event, and a bus event can arrive twice —
    a redelivery, a scheduler flush racing the stream's own completion. Before
    the ledger, the second one delivered the same text again. The ``final``
    row for the batch's agent message is now the record that says "this one is
    already answered".

    The negative is paired with a positive that uses the **same** helper, so
    it cannot pass by the replay being inert: a second replay naming the
    *interrupted* turn's message — which has text but no ``final`` row,
    because nothing was ever delivered as its answer — does go through and
    reach the thread. Same seam, two ids, opposite outcomes; the gate is keyed
    on turn identity and not on the thread.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Dup"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    # ── Phase 1: an ordinary answered turn ────────────────────────────────
    answer = "Answer: the report is attached to Friday's summary."
    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="where is the report?",
            sender_email=user["email"],
        ),
        chat,
        StubAgentEnvConnector(response_text=answer),
    )
    assert resp.status_code == 200
    assert [t for _, t in chat.replaced] == [answer], chat.replaced

    session_id = _channel_session_id(client, headers, agent["id"])
    settled_id = _agent_message_ids(client, headers, session_id)[-1]
    assert _rows(db, channel, thread_key) == [("final", 0, "delivered")]

    # ── Phase 2: the same completion again → nothing ──────────────────────
    replay = _Chat()
    with ExitStack() as stack:
        replay.apply(stack)
        replay_stream_completed(session_id, settled_id)
    assert replay.sent == [] and replay.updated == []
    assert replay.replaced == [] and replay.deleted == []
    assert _rows(db, channel, thread_key) == [("final", 0, "delivered")]

    # ── Phase 3: a turn stopped mid-answer, so it has no final row ────────
    partial = "Partial: I found three candidates so far,"
    stopped = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="find candidates", sender_email=user["email"]
        ),
        stopped,
        _LiveStream(
            events=[
                {
                    "type": "session_created", "content": "",
                    "session_id": "ext-1", "metadata": {},
                },
                {"type": "assistant", "content": partial, "metadata": {}},
                _PAUSE,
                {"type": "interrupted"},
            ]
        ),
    )
    assert resp.status_code == 200
    stopped_id = _agent_message_ids(client, headers, session_id)[-1]
    assert stopped_id != settled_id
    assert "final" not in [
        r.role
        for r in list_turn_deliveries(db, channel["id"], thread_key)
        if str(r.session_message_id) == stopped_id
    ]

    # ── Phase 4: the SAME replay seam, on that id, does deliver ───────────
    second = _Chat()
    with ExitStack() as stack:
        second.apply(stack)
        replay_stream_completed(session_id, stopped_id)
    delivered_text = "\n".join(
        second.sent + [t for _, t in second.updated] + [t for _, t in second.replaced]
    )
    assert partial in delivered_text, (second.sent, second.updated, second.replaced)


# ---------------------------------------------------------------------------
# Seal → final, and the divergence check
# ---------------------------------------------------------------------------


def test_a_sealing_turn_records_one_row_per_message_it_left_standing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """draft → sealed → final, over a real turn, with the kill switch as control.

    The relay writes a row when a **fresh draft message** appears and updates
    that same row in place when the slice it holds is sealed — one row per
    external message, never one per flush. The completion then attributes
    those rows to the turn's agent message, renumbers them densely, and turns
    the last standing draft into the ``final`` row.

    The second half is the control that makes the first half mean something:
    ``CHANNEL_STREAM_UPDATES_ENABLED=False`` drives a byte-identical stream
    with no relay attached, and the same turn leaves exactly one row — the
    ``final`` one the completion writes for itself. So the ``sealed`` row above
    is evidence the relay ran, not an artefact of the harness.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Seal"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    filler = "word " * 14
    para_1 = f"Paragraph one. {filler}"
    para_2 = f"Paragraph two. {filler}"
    para_3 = f"Paragraph three. {filler}"
    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": f"{para_1}\n\n", "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": f"{para_2}\n\n", "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": para_3, "metadata": {}},
        {"type": "done"},
    ]

    # ── Phase 1: the relay seals once, then the reply settles the draft ───
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="three paragraphs please",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
    )
    assert resp.status_code == 200
    # What the reader saw, so the ledger is being compared against a real
    # thread: paragraph one settled into the first message, the rest into the
    # fresh one below it.
    assert (["spaces/AAA/messages/m1", para_1]) == [
        chat.updated[-1][0], chat.updated[-1][1]
    ], chat.updated
    assert chat.replaced == [
        ("spaces/AAA/messages/m2", f"{para_2}\n\n{para_3}")
    ], chat.replaced

    session_id = _channel_session_id(client, headers, agent["id"])
    message_id = _agent_message_ids(client, headers, session_id)[-1]

    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert [(r.role, r.part_index, r.status) for r in rows] == [
        ("sealed", 0, "delivered"),
        ("final", 1, "delivered"),
    ], [(r.role, r.part_index, r.status) for r in rows]
    assert {str(r.session_message_id) for r in rows} == {message_id}
    sealed, final = rows
    # The sealed row names the message it is standing in, and records how far
    # into the answer it reached; the final row records the whole answer.
    assert sealed.external_message_id == "spaces/AAA/messages/m1"
    assert sealed.visible_char_end == len(f"{para_1}\n\n")
    assert sealed.content_sha256
    assert final.external_message_id == "spaces/AAA/messages/m2"
    assert final.visible_char_end == len(f"{para_1}\n\n{para_2}\n\n{para_3}".strip())
    assert final.content_sha256 and final.content_sha256 != sealed.content_sha256

    # ── Phase 2: the control — no relay, one row ──────────────────────────
    other_thread = f"spaces/AAA/threads/{random_lower_string()}"
    plain = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=other_thread, text="three paragraphs please",
            sender_email=user["email"],
        ),
        plain,
        _LiveStream(events=events),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
        CHANNEL_STREAM_UPDATES_ENABLED=False,
    )
    assert resp.status_code == 200
    assert _rows(db, channel, other_thread) == [("final", 0, "delivered")]


def test_a_finalized_answer_that_no_longer_matches_the_seal_is_marked_diverged(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The mismatch arm — observational, and it costs the reader nothing.

    At completion the ledger asks whether the finalized canonical answer still
    starts with what the relay already sealed into the thread: the sealed
    row's ``visible_char_end`` characters, digested and compared. Match →
    silence. Mismatch → a WARNING and the row marked ``diverged``. **Delivery
    is byte-identical either way** — the settled reply is the relay's own
    accumulated text by decision, and this check does not revisit that.

    Phase 1 is the same turn with nothing forced, and it is the load-bearing
    half: it proves an ordinary sealing turn does **not** diverge, so
    ``diverged`` in phase 2 is a difference the check made rather than the
    state every sealing turn is already in.

    Phase 2 forces the mismatch, and the forgery is named rather than hidden.
    Producing a genuine divergence through the product is not currently
    possible — both sides are computed from the same buffer through the same
    ``_visible``, which is exactly why the policy was decided as "log it and
    see whether it ever fires". So what is forged is the **recorded** side:
    ``delivered_prefix_key``, the helper the relay calls to describe the slice
    it just sealed, is made to record a digest of a different string of the
    same length. That is precisely the hypothesised production event — the
    thread is showing text the finalized answer does not begin with — and it
    leaves the code under test (``_check_prefix``, the marking, and every
    delivery decision around it) completely real.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Diverge"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    filler = "word " * 14
    para_1 = f"Paragraph one. {filler}"
    para_2 = f"Paragraph two. {filler}"
    para_3 = f"Paragraph three. {filler}"

    def _stream():
        return [
            {
                "type": "session_created", "content": "",
                "session_id": "ext-1", "metadata": {},
            },
            {"type": "assistant", "content": f"{para_1}\n\n", "metadata": {}},
            _PAUSE,
            {"type": "assistant", "content": f"{para_2}\n\n", "metadata": {}},
            _PAUSE,
            {"type": "assistant", "content": para_3, "metadata": {}},
            {"type": "done"},
        ]

    def _verbs(chat: _Chat) -> tuple[list, list, list, list]:
        """Everything the reader was shown, message ids included."""
        return (chat.sent, chat.updated, chat.replaced, chat.deleted)

    # ── Phase 1: control — the same turn, nothing forced ──────────────────
    match_thread = f"spaces/AAA/threads/{random_lower_string()}"
    match_chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=match_thread, text="three paragraphs please",
            sender_email=user["email"],
        ),
        match_chat,
        _LiveStream(events=_stream()),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
    )
    assert resp.status_code == 200
    match_rows = [
        (r.role, r.status)
        for r in list_turn_deliveries(db, channel["id"], match_thread)
    ]
    assert match_rows == [("sealed", "delivered"), ("final", "delivered")], match_rows

    # ── Phase 2: the same turn, with the sealed prefix mis-recorded ───────
    diverged_thread = f"spaces/AAA/threads/{random_lower_string()}"
    diverged_chat = _Chat()

    def _forged_prefix_key(visible_prefix: str) -> tuple[int, str]:
        from app.services.server_channels.channel_turn_delivery_service import (
            visible_digest,
        )

        normalized = visible_prefix.lstrip()
        # Same length, different content: the length half of the check still
        # passes and the digest half is what fires, which is the shape the
        # policy describes ("no longer starts with the delivered prefix").
        return len(normalized), visible_digest("x" * len(normalized))

    with patch(
        "app.services.server_channels.channel_stream_relay.delivered_prefix_key",
        _forged_prefix_key,
    ):
        resp = _post(
            client,
            channel,
            signer,
            build_message_event(
                thread_key=diverged_thread, text="three paragraphs please",
                sender_email=user["email"],
            ),
            diverged_chat,
            _LiveStream(events=_stream()),
            CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
        )
    assert resp.status_code == 200

    diverged_rows = [
        (r.role, r.status)
        for r in list_turn_deliveries(db, channel["id"], diverged_thread)
    ]
    assert diverged_rows == [("sealed", "diverged"), ("final", "delivered")], (
        diverged_rows
    )

    # ── Phase 3: and the reader could not tell ────────────────────────────
    # The relay's tail was delivered exactly as in the control run: same
    # verbs, same message ids, same text, in the same order. Nothing was
    # re-sent, withdrawn or deleted because of the mismatch.
    assert _verbs(diverged_chat) == _verbs(match_chat), (
        _verbs(diverged_chat),
        _verbs(match_chat),
    )
    assert diverged_chat.deleted == []
    assert get_binding_status_message_id(db, channel["id"], diverged_thread) is None


def test_a_failed_final_delivery_is_retried_and_corrects_its_own_row(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The retry path, and the unique constraint it would otherwise land on.

    The idempotency gate deliberately does **not** count a ``final`` row whose
    delivery *failed*: a row recording a reply that never reached the thread is
    a reason to try again, not a reason to stop. So ``settle_turn`` is
    re-invoked over a turn it has already written a row for — and it has to
    correct that row in place. Inserting a second one lands straight on the
    ``(session_message_id, part_index)`` unique constraint, where the ledger's
    own guards swallow the failure: the correction disappears and the ledger
    keeps claiming the reader never got their answer.

    Driven through the real thing: the adapter's ``replace_message`` raises on
    the first attempt, so the turn genuinely fails to deliver, and the replay
    then runs against a working adapter.
    """
    user, headers, agent = _sender_with_one_agent(
        client, superuser_token_headers, label="Retry"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    answer = "Answer: the invoice was raised on the 3rd."

    # ── Phase 1: the final delivery fails ─────────────────────────────────
    broken = _Chat()
    broken.replace = AsyncMock(side_effect=ChannelSendError("chat is down"))
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="when was it raised?",
            sender_email=user["email"],
        ),
        broken,
        StubAgentEnvConnector(response_text=answer),
    )
    assert resp.status_code == 200
    # The attempt was made and the transport refused it, so nothing was
    # delivered — ``replaced`` records the *call*, not a message the reader
    # ever saw, and no fallback post went out under it either.
    assert [t for _, t in broken.replaced] == [answer], broken.replaced
    assert broken.sent == [REPLY_WORKING], broken.sent
    assert _rows(db, channel, thread_key) == [("final", 0, "failed")]

    session_id = _channel_session_id(client, headers, agent["id"])
    message_id = _agent_message_ids(client, headers, session_id)[-1]

    # ── Phase 2: the same completion again, with a working transport ──────
    # The gate lets it through precisely because the standing row is ``failed``.
    retry = _Chat()
    with ExitStack() as stack:
        retry.apply(stack)
        replay_stream_completed(session_id, message_id)

    assert answer in "\n".join(
        retry.sent + [t for _, t in retry.updated] + [t for _, t in retry.replaced]
    ), (retry.sent, retry.updated, retry.replaced)

    # ── Phase 3: one row, corrected — not two, and not still failed ───────
    rows = list_turn_deliveries(db, channel["id"], thread_key)
    assert [(r.role, r.part_index, r.status) for r in rows] == [
        ("final", 0, "delivered")
    ], [(r.role, r.part_index, r.status) for r in rows]
    assert str(rows[0].session_message_id) == message_id
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None
