"""``/stop`` — the one thing a sender types *at the pipeline*, not at the agent.

A chat thread has nowhere to render the web client's stop button, so the stop
is a word. ``channel_control_commands`` is a registry of exactly one entry
today, and ``ChannelInboundService.process_inbound`` consults it at step 7a —
**below** the thread-ownership decline and below every security gate, which is
the whole authorization argument for calling ``MessageService.interrupt_stream``
without re-deriving access (that method's documented contract is "the caller
authorizes"). Invariant 6 of the plan is precisely that ordering, and it is
pinned here by a stranger's ``/stop`` getting the ordinary "this conversation
belongs to someone else" decline and never reaching the command at all.

The second half of the file is the **acknowledgement**: a successful ``/stop``
deliberately replies nothing, because ``ChannelOutboundService
.handle_stream_interrupted`` settles the thread's status notice with whatever
the agent had already said plus a stopped marker, and that message *is* the
answer. Four shapes of stopped turn are covered, and they are not
interchangeable — two of them shipped as bugs in opposite directions against a
fully green suite, because every pre-existing test drove a relay that had
produced text:

* the agent had said something → partial answer, marker under it;
* **the agent had said nothing at all** — the likeliest stop there is, and the
  one that stranded the spinner;
* the relay had already sealed everything it held → the marker as its own
  message, below the sealed text (the shape that settled a bare marker over a
  live draft);
* the thread was never narrating → **silence**, because posting into a thread
  that was showing nothing is worse than saying nothing.

Every assertion below is on what reaches the thread through the four adapter
verbs, never on relay internals, so a refactor of that seam does not have to
edit this file to keep it honest.

Matching itself (``"/stop"`` vs ``" /STOP "`` vs ``"/stopx"``) is pure string
logic; the API surface here exercises the normalized and the plain form and
leaves the rest to the module's own unit coverage.
"""
import types
import uuid
from contextlib import ExitStack
from itertools import count
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.server_channels.adapters.base import (
    ChannelReplaceResult,
    ChannelSendError,
)
from app.services.server_channels.channel_inbound_service import (
    REPLY_NOTHING_TO_STOP,
    REPLY_THREAD_OWNED,
    REPLY_WORKING,
    REPLY_WORKING_ON_IT,
)
from app.services.server_channels.channel_outbound_service import (
    STOPPED_NOTICE,
    STOPPED_SUFFIX,
)
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.environment import set_environment_status
from tests.utils.message import list_messages
from tests.utils.routing import refuse_to_classify
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_attachment,
    build_message_event,
    create_server_channel,
    flush_pending_bindings,
    get_binding_status_message_id,
    post_webhook,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_ADAPTER = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"
_FETCH_TARGET = f"{_ADAPTER}.fetch_attachment"
_INTERRUPT_TARGET = (
    "app.services.sessions.message_service.MessageService.interrupt_stream"
)

_SECRETS = '{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "x"}'

#: Consumed rather than yielded by ``_LiveStream``: "let the event loop run".
#: Same marker and the same reason as in
#: ``server_channels_streaming_updates_test.py`` — the relay's flusher is a
#: sibling task and only runs when the stream yields.
_PAUSE = {"type": "__pause__"}


class _LiveStream(StubAgentEnvConnector):
    """``StubAgentEnvConnector`` that hands the event loop a turn at ``_PAUSE``."""

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append({"base_url": base_url, "payload": payload})
        for event in self.events:
            if event is _PAUSE:
                import asyncio

                await asyncio.sleep(0)
                continue
            yield event


class _Chat:
    """The four outbound verbs, mocked together, with serial message ids.

    Same shape as ``server_channels_status_notice_test.py::_Chat`` (all four
    verbs, a real-looking ``spaces/AAA/messages/…`` id from ``send_message``,
    a ``ChannelReplaceResult`` from ``replace_message``), with serial ids so a
    turn that ends up owning two messages can say which one was written into.
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


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*", secrets=_SECRETS)
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _sender_with_one_agent(client, superuser_headers, *, label="Stop"):
    """A sender who owns exactly one eligible agent (Pass 1's `only_one`)."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _post(client, channel, signer, event, chat: _Chat, stub, *, extras=None, **overrides):
    """One verified webhook delivery, drained, with the four verbs observed.

    ``extras`` is a callable handed the ``ExitStack`` so a test can add its own
    patches (``interrupt_stream``, ``fetch_attachment``) inside the same scope
    the drain runs in — the background task is where the work happens, so a
    patch that only wraps ``post_webhook`` would be inactive for all of it.
    """
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
        added = extras(stack) if extras else {}
        resp = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        drain_tasks()
    return resp, added


def _bind_thread(client, channel, signer, user, thread_key, stub) -> _Chat:
    """First turn on a thread: routes, answers, leaves an ACTIVE binding."""
    chat = _Chat()
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        chat,
        stub,
    )
    assert chat.replaced, "setup: the first turn should have delivered a reply"
    return chat


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_stop_interrupts_the_thread_and_never_reaches_the_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The happy path, and the two things that make it a *command*.

    1. ``MessageService.interrupt_stream`` is called for the thread's own bound
       session — patched here, because whether the interrupt itself works is
       that method's business and not this pipeline's.
    2. The word ``/stop`` is **not ingested**: no second `stream_chat`, and no
       user message on the session. A command that also reached the agent would
       have it answering "I'll stop" into a turn that no longer exists.
    3. The webhook acks ``{}`` — silence, like every other accepted message.
       The command says whatever it has to say from its own background task.

    Success is also silent on the thread: the acknowledgement is the stopped
    marker the ``STREAM_INTERRUPTED`` subscriber settles into the notice, which
    the second half of this file covers. Here the interrupt is mocked away, so
    no such event is emitted and the correct outcome is nothing at all.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="first answer")

    _bind_thread(client, channel, signer, user, thread_key, stub)
    session = next(
        s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]
    )
    messages_before = list_messages(client, headers, session["id"])

    chat = _Chat()
    resp, added = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="/stop", sender_email=user["email"]
        ),
        chat,
        stub,
        extras=lambda stack: {
            "interrupt": stack.enter_context(
                patch(_INTERRUPT_TARGET, AsyncMock(return_value=None))
            )
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {}, resp.json()

    # ── The interrupt was asked for, on this thread's session ─────────────
    interrupt = added["interrupt"]
    assert interrupt.await_count == 1, interrupt.await_args_list
    assert str(interrupt.await_args.kwargs["session_id"]) == session["id"]
    assert interrupt.await_args.kwargs["environment_id"] is not None

    # ── The agent never saw the word ──────────────────────────────────────
    assert len(stub.stream_calls) == 1, [c["payload"] for c in stub.stream_calls]
    assert list_messages(client, headers, session["id"]) == messages_before

    # ── And nothing was posted: the marker is the acknowledgement ─────────
    assert chat.sent == [] and chat.updated == [] and chat.replaced == []
    assert chat.deleted == []


def test_stop_with_nothing_running_says_exactly_that(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``interrupt_stream`` raises ``ValueError`` for an idle session.

    Not patched here — the previous turn finished, so the real method really
    does find nothing to interrupt, which is the whole point: the decline is
    produced by the production path rather than by a mock. The sender is told
    once, through ``_reply``, and the agent still never sees the text.

    ``" /STOP "`` rather than ``"/stop"``: the matcher strips and casefolds,
    and a person typing into Chat gets both wrong regularly.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="first answer")

    _bind_thread(client, channel, signer, user, thread_key, stub)

    chat = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text=" /STOP ", sender_email=user["email"]
        ),
        chat,
        stub,
    )

    assert resp.status_code == 200
    assert resp.json() == {}, resp.json()
    assert chat.sent == [REPLY_NOTHING_TO_STOP], chat.sent
    # No spinner, no draft, no reply — the decline is the whole turn.
    assert chat.updated == [] and chat.replaced == [] and chat.deleted == []
    assert len(stub.stream_calls) == 1


def test_a_stranger_is_declined_before_stop_is_ever_a_command(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Invariant 6: interception sits strictly AFTER the ownership gate.

    Someone else's thread answers a stranger with the ordinary
    ``REPLY_THREAD_OWNED`` decline, synchronously, and the command registry is
    never consulted — ``interrupt_stream`` is patched purely so that a
    regression which moved the interception above the gate would be caught by
    the call count rather than by a downstream symptom.

    That ordering is not cosmetic: the interception point is where the
    authorization for ``interrupt_stream`` comes from. Above the gate, any
    verified sender who guessed a thread key could stop somebody else's turn.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    stranger, stranger_headers, _ = _sender_with_one_agent(
        client, superuser_token_headers, label="Stranger"
    )
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="first answer")

    _bind_thread(client, channel, signer, user, thread_key, stub)

    chat = _Chat()
    resp, added = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="/stop", sender_email=stranger["email"]
        ),
        chat,
        stub,
        extras=lambda stack: {
            "interrupt": stack.enter_context(
                patch(_INTERRUPT_TARGET, AsyncMock(return_value=None))
            )
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "text": REPLY_THREAD_OWNED,
        "thread": {"name": thread_key},
    }, resp.json()
    assert added["interrupt"].await_count == 0
    # Nothing at all was written into the owner's thread.
    assert chat.sent == [] and chat.updated == [] and chat.replaced == []
    # And the stranger got no session out of it.
    assert list_sessions(client, stranger_headers) == []


def test_a_redelivered_stop_is_acknowledged_once(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Google Chat retries. The command must not run twice.

    The bound-thread paths tolerate a retry because ``_continue_thread`` stamps
    ``last_external_message_id`` only after a successful ingest — this branch
    stamps nothing and commits nothing, so it carries its own dedup on a
    ``:control:`` key namespace. Without it the second delivery finds the
    stream it just stopped already gone and posts "there's nothing running
    right now" directly under the stopped marker: the doubled acknowledgement
    the silent-success design exists to avoid.

    Both deliveries carry the same ``message.name``, which is what Chat's own
    retry does, and both are queued before the drain so the dedup is exercised
    against a genuinely repeated event rather than a re-run test.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="first answer")

    _bind_thread(client, channel, signer, user, thread_key, stub)

    # A fixed id, since a retry is the SAME message arriving twice. The dedup
    # key embeds the channel id, and both channel and message id are unique to
    # this test, so the process-global recent-ids map cannot cross-talk.
    event = build_message_event(
        thread_key=thread_key,
        text="/stop",
        sender_email=user["email"],
        message_name=f"spaces/AAA/messages/retry-{uuid.uuid4().hex}",
    )

    chat = _Chat()
    token = signer.token(audience=channel["config"]["project_number"])
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(settings, "CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS", 0)
        )
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, refuse_to_classify))
        chat.apply(stack)
        first = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        second = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        drain_tasks()

    assert first.status_code == 200 and first.json() == {}
    # The retry is acked exactly like the original — a webhook that answered
    # differently would keep Chat retrying.
    assert second.status_code == 200 and second.json() == {}
    # One acknowledgement, not two.
    assert chat.sent == [REPLY_NOTHING_TO_STOP], chat.sent


# ---------------------------------------------------------------------------
# The three shapes that are NOT a command
# ---------------------------------------------------------------------------


def test_stop_as_the_first_message_of_a_thread_is_an_ordinary_message(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deliberately not intercepted — documented behaviour, pinned so it stays.

    There is no binding on a brand-new thread and therefore nothing to stop.
    Interception happens at step 7a, which only runs for an existing binding,
    so a first-message ``/stop`` falls through to routing like any other text
    and the agent answers it. That reads like an oversight and is not one:
    intercepting here would mean answering "there's nothing running right now"
    to somebody who has never talked to us, instead of routing them to an
    assistant.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    reply = "Nothing is running yet — what would you like me to do?"
    stub = StubAgentEnvConnector(response_text=reply)

    chat = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="/stop", sender_email=user["email"]
        ),
        chat,
        stub,
    )

    assert resp.status_code == 200
    # Routed, streamed, answered — the full first-contact story.
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert [t for _, t in chat.updated] == [REPLY_WORKING_ON_IT], chat.updated
    assert chat.replaced == [("spaces/AAA/messages/m1", reply)], chat.replaced
    # The agent really was handed the literal text.
    assert len(stub.stream_calls) == 1
    assert stub.stream_calls[0]["payload"]["message"] == "/stop"
    assert REPLY_NOTHING_TO_STOP not in chat.outbound_text


def test_stop_with_a_file_attached_is_an_ordinary_message(
    client: TestClient, superuser_token_headers: dict[str, str], tmp_path
) -> None:
    """A command takes no arguments and no files.

    Somebody who attached a file meant it to be read, so the whole message goes
    to the agent — text included — rather than being swallowed by a branch that
    would silently drop the attachment along with it. The guard is
    ``not file_ids``, and this drives a genuinely materialised attachment so
    ``file_ids`` is really non-empty rather than merely believed to be.

    ``UPLOAD_BASE_PATH`` must end in ``uploads``: ``FileStorageService`` stores
    a path relative to the *parent* directory and re-resolves it that way, so a
    differently-named root round-trips to a file the env upload cannot find.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="answer")

    uploads = str(tmp_path / "uploads")
    fetch = lambda stack: {  # noqa: E731 — one expression, used once
        "fetch": stack.enter_context(
            patch(_FETCH_TARGET, AsyncMock(return_value=b"attached bytes"))
        )
    }

    _bind_thread(client, channel, signer, user, thread_key, stub)
    session = next(
        s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]
    )

    chat = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key,
            text="/stop",
            sender_email=user["email"],
            attachments=[
                build_message_attachment(
                    content_name="notes.txt", content_type="text/plain"
                )
            ],
        ),
        chat,
        stub,
        extras=fetch,
        UPLOAD_BASE_PATH=uploads,
    )

    assert resp.status_code == 200
    # Ingested, not intercepted: a second LLM turn ran...
    assert len(stub.stream_calls) == 2, [c["payload"] for c in stub.stream_calls]
    # ...and the message is on the session, with its file, text intact.
    user_messages = [
        m for m in list_messages(client, headers, session["id"]) if m["role"] == "user"
    ]
    assert [m["content"] for m in user_messages] == ["hello", "/stop"], user_messages
    assert len(user_messages[-1]["files"]) == 1, user_messages[-1]
    assert REPLY_NOTHING_TO_STOP not in chat.outbound_text


def test_stop_while_the_thread_is_still_installing_is_never_parked(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A PENDING_INSTALL binding answers "nothing running" and queues nothing.

    Interception runs for a pending binding as well as an active one, and the
    reason is the *parking*, not the reply: everything that reaches step 7 on a
    pending binding is parked and replayed at the agent the moment the install
    finishes. A parked ``/stop`` would arrive minutes later as the first thing
    the brand-new assistant is asked to do.

    So the flush is driven to completion here and the agent's transcript
    checked: it must contain the original request and nothing else.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(
        client, publisher_headers, name=f"StopBundle-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, publisher_headers, agent["id"], "Handle stop-flow requests"
    )
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    bundle_uuid = client.get(
        f"{API}/agents/{agent['id']}", headers=publisher_headers
    ).json()["bundle_uuid"]

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="installed and answering")
    token = signer.token(audience=channel["config"]["project_number"])
    classify_result = types.SimpleNamespace(
        agent_id=bundle_uuid, transformed_message=None
    )

    # ── Phase 1: first contact triggers the install; the binding is pending ─
    install = _Chat()
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=classify_result))
        install.apply(stack)
        post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=thread_key,
                text="please summarise the report",
                sender_email=consumer["email"],
            ),
            bearer_token=token,
        )
        drain_tasks()
    assert install.sent == [REPLY_WORKING], install.sent
    assert "Setting up" in install.updated[0][1], install.updated

    # ── Phase 2: /stop on the pending thread ──────────────────────────────
    stop = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="/stop", sender_email=consumer["email"]
        ),
        stop,
        stub,
    )
    assert resp.status_code == 200
    assert stop.sent == [REPLY_NOTHING_TO_STOP], stop.sent
    # The install notice is untouched — the decline is a message of its own,
    # not a rewrite of the thing narrating the setup.
    assert stop.updated == [], stop.updated

    # ── Phase 3: the install finishes and drains the parked messages ───────
    installed = next(
        a
        for a in client.get(f"{API}/agents/", headers=consumer_headers).json()["data"]
        if a["bundle_uuid"] == bundle_uuid
    )
    set_environment_status(db, installed["active_environment_id"], "running")
    db.commit()

    flush = _Chat()
    with ExitStack() as stack:
        stack.enter_context(patch(_STREAM_TARGET, stub))
        flush.apply(stack)
        advanced = flush_pending_bindings(db)
        drain_tasks()

    assert advanced == 1
    # Exactly one message was ever handed to the agent, and it is the one the
    # sender actually wrote. The `/stop` was answered and dropped, never queued.
    assert [c["payload"]["message"] for c in stub.stream_calls] == [
        "please summarise the report"
    ]


# ---------------------------------------------------------------------------
# What a stopped turn shows the reader
# ---------------------------------------------------------------------------


def test_a_stopped_answer_keeps_what_was_said_and_marks_the_stop(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``STREAM_INTERRUPTED`` settles the notice: partial answer, marker under.

    This is the acknowledgement a successful ``/stop`` leans on instead of
    replying — and it is emitted *instead of* ``STREAM_COMPLETED``, so before
    the subscriber existed an interrupted channel turn left its notice stranded
    on "💬 Working on your message…" until the next turn patched it, telling
    the person we were still busy with something they had cancelled.

    The half-answer must survive: the draft is the notice, so replacing it with
    a bare marker would take back the text the reader watched arrive.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    partial = "I found three candidates so far, the first is"
    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": partial, "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]

    chat = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="find me candidates",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
    )

    assert resp.status_code == 200
    notice_id = "spaces/AAA/messages/m1"
    # The partial was on screen while the agent was still writing…
    assert (notice_id, partial) in chat.updated, chat.updated
    # …and the stop is written under it, into the same message.
    assert chat.replaced == [
        (notice_id, f"{partial}\n\n{STOPPED_SUFFIX}")
    ], chat.replaced
    assert chat.deleted == [], chat.deleted
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


def test_a_turn_stopped_before_the_agent_spoke_still_says_so(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The likeliest stop there is — and the one that shipped broken twice.

    Somebody sends a message, thinks better of it, and types ``/stop`` before
    the agent has produced a single token. A relay is attached and registered;
    it has simply never been fed. Two mirror-image bugs shipped on exactly this
    shape against a fully green suite, because every pre-existing test drove a
    relay that had produced text — one left the spinner standing forever, the
    other settled a bare marker over a live draft.

    What is asserted is only what reaches the thread: the spinner is gone, and
    the message it was standing in now says the turn was stopped. No relay
    state, no helper return value, nothing that a refactor of that seam could
    make stale while leaving the reader with a stuck spinner.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]

    chat = _Chat()
    resp, _ = _post(
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
    # One message on the thread, and its LAST state is the acknowledgement.
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert [mid for mid, _ in chat.updated] == [notice_id, notice_id], chat.updated
    assert [t for _, t in chat.updated] == [
        REPLY_WORKING_ON_IT,
        STOPPED_NOTICE,
    ], chat.updated
    # Not deleted (no "message deleted by its author" tombstone) and not
    # replaced by an answer that never existed.
    assert chat.replaced == [] and chat.deleted == []
    # Settled: the id is released, so the next turn opens its own notice
    # instead of patching this acknowledgement away.
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


def test_the_stop_marker_lands_below_text_the_relay_already_sealed(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Everything already on screen → the marker as its own message.

    A turn long enough to seal leaves the sealed text standing as a finished
    message and releases the notice id. If the stop then arrives with nothing
    left in the draft, there is no slot to settle into — so the marker is
    posted as a fresh message *below* the answer, rather than overwriting it.

    This is the ``("", True)`` shape: the relay is healthy and has delivered
    everything it held. Distinguishing it from "the relay broke and may be
    holding the only copy" is what the interrupt handler asks
    ``_take_stream_tail_ex`` for; getting it wrong here costs the reader the
    sealed paragraph, permanently, since nothing else re-delivers it.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    # A paragraph, its break, and then only whitespace: the seal consumes the
    # break and the remainder is not worth drawing, so the draft ends empty
    # while the sealed message stands.
    sealed = "Sealed paragraph one, long enough to clear the seal floor here."
    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": f"{sealed}\n\n   ", "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]
    # Derived from the text rather than a magic number, so an editor rewording
    # the paragraph cannot silently turn this into a no-seal turn. The draft is
    # ``len(sealed) + 5`` characters (paragraph, blank line, three spaces); a
    # threshold two above the paragraph itself puts the draft over the line
    # while leaving the paragraph break inside the seal search window.
    seal_target = len(sealed) + 2

    chat = _Chat()
    resp, _ = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="write me a paragraph",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=seal_target,
    )

    assert resp.status_code == 200
    notice_id = "spaces/AAA/messages/m1"
    # The notice became the sealed paragraph…
    assert chat.updated == [
        (notice_id, REPLY_WORKING_ON_IT),
        (notice_id, sealed),
    ], chat.updated
    # …and the stop is a NEW message under it, not a rewrite of it.
    assert chat.sent == [REPLY_WORKING, STOPPED_NOTICE], chat.sent
    assert chat.replaced == [] and chat.deleted == []
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


def test_a_thread_that_was_never_narrating_is_told_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """No standing notice → silence, even though a relay was attached.

    The opening notice fails to post here (``send_message`` raises, which is
    what a transport hiccup or a revoked credential looks like), so the binding
    never adopts an id and the thread is showing nothing at all. When the turn
    is then stopped, the handler must NOT post a stopped marker: a thread that
    never saw a turn start gets no message about the turn ending.

    That branch is also what holds the **email** transport at zero behaviour
    change — a transport with no progress surface can never hold a notice id,
    so it reaches this same return by construction rather than by a check
    somebody has to remember. The shape is reproduced here on Google Chat
    because email declines the relay a layer earlier and would not exercise the
    ``("", False)`` half at all.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        _PAUSE,
        {"type": "interrupted"},
    ]

    chat = _Chat()
    chat.send = AsyncMock(side_effect=ChannelSendError("the notice could not post"))
    resp, _ = _post(
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
    # The pipeline tried to open a notice and could not; nothing else was
    # attempted, and in particular the stopped marker was never written by any
    # verb. (Compare the test above, where the same stream on a thread WITH a
    # standing notice gets the marker.)
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert STOPPED_NOTICE not in chat.outbound_text, chat.outbound_text
    assert chat.updated == [] and chat.replaced == [] and chat.deleted == []
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None
