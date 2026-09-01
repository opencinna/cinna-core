"""The status notice, streaming — one message that grows while the agent writes.

Before this feature a Google Chat turn was silent for as long as the agent
worked: the notice said "💬 Working on your message…" and then the whole reply
arrived at once. ``channel_stream_relay`` tees off the same stream the web
client watches and rewrites that one message every few seconds with the text
accumulated so far, so the reader watches the answer appear instead of watching
a spinner. When the draft grows past what one Chat message can hold it is
**sealed** — left standing, finished — and a fresh draft opens below it.

What this file is for, and what it deliberately is not: the relay's own
decisions (debounce, seal arithmetic, tail idempotency, fence re-opening) are
pure logic and are unit-tested in ``tests/unit/test_channel_stream_relay.py``
and ``tests/unit/test_chat_text_chunking.py``. Everything here is the
**integration**: a real webhook delivery, a real routing decision, a real
session, the real outbound service, and the four Google Chat verbs mocked at
the adapter boundary — the same seam ``server_channels_status_notice_test.py``
uses, and the same ``_Chat`` shape. What is asserted is only what a person
reading the thread would see.

Two facts about the harness that are load-bearing enough to state up front:

* **``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS = 0``.** The setting documents
  ``<= 0`` as "flush immediately on every event", which exists precisely so a
  test never has to sleep to watch a draft update. It is set through
  ``patch.object(settings, ...)``, the override pattern this directory already
  uses for ``UPLOAD_BASE_PATH`` and ``CHANNEL_ATTACHMENT_MAX_FILE_MB``.

* **``_LiveStream``, and why ``StubAgentEnvConnector`` alone is not enough.**
  The relay's flusher is a *sibling asyncio task*: it can only run when the
  streaming coroutine yields to the event loop. A real SSE stream yields on
  every network read; ``StubAgentEnvConnector`` yields its canned events with
  no ``await`` between them, so under it the loop never gets a turn and the
  draft is never patched — the whole answer lands in one piece and every
  assertion below would pass vacuously by measuring the *old* behaviour. The
  subclass here inserts an explicit ``_PAUSE`` marker where the test author
  wants the reader to see a redraw; it is not a hack around the relay, it is
  the one thing the canned stub does not model about a network.

Interrupted turns — what the thread is shown when a stream is *stopped* rather
than completed — live next door in ``server_channels_stop_command_test.py``,
because the acknowledgement is what makes ``/stop`` visible.
"""
import asyncio
from contextlib import ExitStack
from itertools import count
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.server_channels.adapters.base import ChannelReplaceResult
from app.services.server_channels.channel_inbound_service import (
    REPLY_WORKING,
    REPLY_WORKING_ON_IT,
)
from app.services.server_channels.channel_outbound_service import TURN_FAILED_TEXT
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.routing import refuse_to_classify
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    get_binding_status_message_id,
    post_webhook,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

_ADAPTER = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"

#: A service-account blob, so the channel reads as having an outbound
#: credential. Same reason as in ``server_channels_status_notice_test.py``: a
#: channel that cannot post has no notice to roll, so it would have no draft
#: either and every test here would measure the wrong thing.
_SECRETS = '{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "x"}'

#: Consumed by ``_LiveStream`` rather than yielded: "let the event loop run
#: here". See the module docstring — this is where the reader sees a redraw.
_PAUSE = {"type": "__pause__"}


class _LiveStream(StubAgentEnvConnector):
    """``StubAgentEnvConnector`` that gives the relay's flusher a turn.

    Identical to its parent except that a ``_PAUSE`` entry in ``events`` is
    not yielded — it awaits ``asyncio.sleep(0)``, handing control to the event
    loop exactly once. That is enough for the flusher task to wake, take the
    relay lock, decide about sealing, and patch the draft, because with the
    interval at 0 none of those steps waits on anything.
    """

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append({"base_url": base_url, "payload": payload})
        for event in self.events:
            if event is _PAUSE:
                await asyncio.sleep(0)
                continue
            yield event


class _Chat:
    """The four outbound verbs, mocked together and read back by text.

    Mirrors ``server_channels_status_notice_test.py::_Chat`` — a test that
    wants the real notice behaviour must mock all four, and ``send_message``
    must hand back a real-shaped ``spaces/AAA/messages/…`` id or the adapter
    refuses it and every state degrades to a fresh post.

    The one difference: ids are **serial** here (``…/m1``, ``…/m2``), not
    constant. A sealing turn ends up owning two messages at once — the sealed
    one and the fresh draft below it — and a constant id would make "the reply
    was written into the new draft, not over the sealed text" unsayable.
    """

    def __init__(self) -> None:
        self._ids = count(1)
        self.send = AsyncMock(side_effect=self._next_id)
        self.update = AsyncMock(return_value=None)
        # A `ChannelReplaceResult`, not a bare id: `_deliver` reads `.replaced`
        # to decide whether the notice was really taken over, and releases the
        # binding's id only when it was.
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

    # `send_message(channel, thread_key, text)`;
    # `update_message(channel, thread_key, message_id, text)`;
    # `replace_message(channel, thread_key, message_id, text)`.
    @property
    def sent(self) -> list[str]:
        return [c.args[-1] or "" for c in self.send.await_args_list]

    @property
    def updated(self) -> list[tuple[str, str]]:
        """(message patched, text)."""
        return [(c.args[2], c.args[3] or "") for c in self.update.await_args_list]

    @property
    def replaced(self) -> list[tuple[str, str]]:
        """(message the text was written into, text)."""
        return [(c.args[2], c.args[3] or "") for c in self.replace.await_args_list]

    @property
    def deleted(self) -> list[str]:
        return [c.args[-1] for c in self.delete.await_args_list]

    @property
    def outbound_text(self) -> str:
        """Everything this thread was shown, concatenated.

        For the negative assertions: a string that appears nowhere in here
        never reached the reader by any verb.
        """
        return "\n".join(
            self.sent
            + [t for _, t in self.updated]
            + [t for _, t in self.replaced]
        )


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*", secrets=_SECRETS)
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _sender_with_one_agent(client, superuser_headers):
    """A sender who owns exactly one eligible agent.

    Pass 1's `only_one` short-circuit with an empty auto-install list, so no
    classifier answer is ever needed and the stub below can raise if one is
    reached after all.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"Stream-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _post(client, channel, signer, event, chat: _Chat, stub, **overrides):
    """One webhook delivery with the four outbound verbs under observation.

    ``CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS`` defaults to 0 here (see the
    module docstring); anything else a test wants to move — the kill switch,
    the seal threshold — is passed through ``overrides``.
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
        resp = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        drain_tasks()
    return resp


def _three_chunk_stream() -> list[dict]:
    """An answer that arrives in three pieces, with the loop free between them.

    Shared by the enabled and disabled scenarios so the two are a genuine
    contrast rather than two differently-shaped streams.
    """
    return [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": "First piece. ", "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": "Second piece. ", "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": "Third piece.", "metadata": {}},
        {"type": "done"},
    ]


_FULL_ANSWER = "First piece. Second piece. Third piece."


# ---------------------------------------------------------------------------
# The rolling draft
# ---------------------------------------------------------------------------


def test_the_draft_grows_in_place_while_the_agent_writes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The headline: one message, patched as the answer arrives, then finished.

    1. The thread gets its usual single notice (a `send`), patched to
       "working on your message…" while routing runs.
    2. Each further chunk of assistant text patches that SAME message with
       everything accumulated so far — so the reader watches one message grow,
       and the intermediate states are prefixes of the final answer rather than
       fragments of it.
    3. The completion writes the whole answer into the same slot and releases
       the notice id, exactly as a non-streaming turn does.

    The prefix property is what is actually asserted about the intermediate
    text, and it is the thing a reader would notice breaking: a draft that
    patched only the newest chunk would leave the message flickering between
    unrelated sentences instead of growing.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="tell me three things", sender_email=user["email"]
        ),
        chat,
        _LiveStream(events=_three_chunk_stream()),
    )

    assert resp.status_code == 200
    assert resp.json() == {}, resp.json()

    # ── One message for the whole turn ────────────────────────────────────
    assert chat.sent == [REPLY_WORKING], chat.sent
    notice_id = "spaces/AAA/messages/m1"
    assert chat.deleted == [], chat.deleted

    # ── The pipeline's own state, then the draft, all patches of that one ──
    assert [mid for mid, _ in chat.updated] == [notice_id] * 3, chat.updated
    texts = [text for _, text in chat.updated]
    assert texts[0] == REPLY_WORKING_ON_IT, texts

    # The draft updates land BEFORE the completion — they are `update_message`
    # calls, and the reply is a `replace_message`. Their content is the answer
    # so far: strictly growing, and each a prefix of the finished text.
    drafts = texts[1:]
    assert drafts == ["First piece. ", "First piece. Second piece. "], drafts
    assert all(_FULL_ANSWER.startswith(d) for d in drafts), drafts
    assert len(drafts[0]) < len(drafts[1]), drafts

    # ── The finished answer takes the same slot ───────────────────────────
    assert chat.replaced == [(notice_id, _FULL_ANSWER)], chat.replaced

    # ── And the id is let go, so the next turn opens a new notice ─────────
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


def test_the_models_thinking_never_reaches_the_thread(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``thinking`` is a distinct stream event type, not a flavour of output.

    It carries the model's reasoning, and it must never appear in a channel
    message. The relay's handler filters on ``type == "assistant"``, which is
    the whole guard — so this drives a stream that interleaves the two and
    checks every byte the four outbound verbs were handed.

    The negative assertion is paired with a positive one on purpose: the
    assistant halves either side of the thinking block must BOTH arrive, and
    arrive **adjacent**. A relay that dropped everything would satisfy "the
    secret never appeared" while saying nothing at all, and a relay that let
    the thinking through would break the adjacency rather than only the
    substring check.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    secret = "the-user-is-probably-lying-about-their-deadline"
    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": "Before. ", "metadata": {}},
        _PAUSE,
        {"type": "thinking", "content": secret, "metadata": {}},
        _PAUSE,
        {"type": "assistant", "content": "After.", "metadata": {}},
        {"type": "done"},
    ]

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="think about it", sender_email=user["email"]
        ),
        chat,
        _LiveStream(events=events),
    )

    assert resp.status_code == 200
    assert secret not in chat.outbound_text, chat.outbound_text
    # Both halves arrived, with nothing between them — so the thinking block
    # was filtered out of the buffer, not merely absent from a lost turn.
    assert chat.replaced[-1][1] == "Before. After.", chat.replaced
    # And the pause after the thinking event did produce a flush, so the
    # filter really was exercised on a draft the reader could have seen.
    assert [t for _, t in chat.updated][1:] == ["Before. "], chat.updated


def test_a_long_answer_seals_the_draft_and_opens_a_fresh_one(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Past the size threshold the draft is finished and a new one starts.

    ``CHANNEL_STREAM_SEAL_TARGET_CHARS`` is a production knob; it is lowered
    here so an ordinary three-paragraph answer crosses it, rather than pumping
    3400 characters through a webhook to observe a decision that does not
    depend on the volume.

    The sequence the reader sees:

    1. the draft holds paragraph one,
    2. the draft is rewritten one last time (**settled**) with paragraph one
       cut at the blank line — a finished message, no trailing blank —
    3. a NEW message opens below it with paragraph two,
    4. the finished answer is written into that new message, and it contains
       the paragraphs after the seal and **not** the sealed one.

    Step 4 is also how the repointing of ``binding.status_message_id`` is
    observed: ``_deliver`` writes into whatever id the binding holds, so a
    reply landing in the fresh draft is the row having been moved onto it. A
    binding still pointing at the sealed message would have overwritten
    paragraph one with the tail — the reader would lose the first half of the
    answer and see the second half twice.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

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

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="write me three paragraphs",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
        CHANNEL_STREAM_SEAL_TARGET_CHARS=120,
    )

    assert resp.status_code == 200
    sealed_id = "spaces/AAA/messages/m1"
    fresh_id = "spaces/AAA/messages/m2"

    # ── Two messages exist by the end: the sealed one and the draft ───────
    assert chat.sent == [REPLY_WORKING, f"{para_2}\n\n"], chat.sent
    assert chat.deleted == [], chat.deleted

    # ── Everything patched before the seal patched the FIRST message ──────
    assert [mid for mid, _ in chat.updated] == [sealed_id] * 3, chat.updated
    working, growing, settled = [t for _, t in chat.updated]
    assert working == REPLY_WORKING_ON_IT
    assert growing == f"{para_1}\n\n"
    # The seal cuts at the paragraph break and consumes it: a finished message
    # does not end in a blank line, and paragraph two is NOT in it.
    assert settled == para_1, settled
    assert para_2 not in settled

    # ── The finished answer goes into the FRESH draft ─────────────────────
    assert chat.replaced == [(fresh_id, f"{para_2}\n\n{para_3}")], chat.replaced
    assert para_1 not in chat.replaced[0][1]

    # ── And that draft's id is released when the reply takes its slot ─────
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


# ---------------------------------------------------------------------------
# The kill switch, and the failure edge
# ---------------------------------------------------------------------------


def test_the_kill_switch_restores_todays_behaviour_exactly(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``CHANNEL_STREAM_UPDATES_ENABLED=False`` — the regression guard.

    This is the assertion that protects **every existing channel reply**, not
    just an off switch: with the relay declining to attach, the turn must take
    the pre-relay path byte for byte — one notice, the pipeline's own two
    states, one `replace_message` carrying the whole stored answer, nothing
    deleted, id released. Strict list equality on all four verbs, deliberately:
    an extra `update_message` here would mean the relay attached anyway, and a
    missing `replace_message` would mean the fallback full-text delivery was
    skipped (the exact hazard ``maybe_attach_channel_relay`` removes stale
    registry entries to avoid — a reader would get *nothing at all*).

    The stream is character-for-character the one
    ``test_the_draft_grows_in_place_while_the_agent_writes`` drives, pauses
    included, so the difference between the two tests is the setting and only
    the setting.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="tell me three things", sender_email=user["email"]
        ),
        chat,
        _LiveStream(events=_three_chunk_stream()),
        CHANNEL_STREAM_UPDATES_ENABLED=False,
    )

    assert resp.status_code == 200
    assert resp.json() == {}, resp.json()

    notice_id = "spaces/AAA/messages/m1"
    assert chat.sent == [REPLY_WORKING], chat.sent
    # The ONLY patch is the pipeline's own progress state. No draft.
    assert chat.updated == [(notice_id, REPLY_WORKING_ON_IT)], chat.updated
    assert chat.replaced == [(notice_id, _FULL_ANSWER)], chat.replaced
    assert chat.deleted == [], chat.deleted
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None


def test_a_stream_that_fails_keeps_the_half_answer_and_apologises_under_it(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The apology goes *under* what the reader already watched arrive.

    ``handle_stream_error`` used to replace the notice with a bare "something
    went wrong". With a relay attached that would take back the half-answer
    on screen — so the failure text is appended to the tail instead, and the
    two are settled into the notice as one message.

    The half-answer is proven to have been visible first (it is one of the
    draft patches), which is what makes the "under it" ordering meaningful
    rather than an accident of string concatenation.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    partial = "Here is the first half of the answer."
    events = [
        {"type": "session_created", "content": "", "session_id": "ext-1", "metadata": {}},
        {"type": "assistant", "content": partial, "metadata": {}},
        _PAUSE,
        # `stream_message_with_events` turns this into a STREAM_ERROR bus
        # event and ends the stream — the same shape a provider outage has.
        {
            "type": "error",
            "content": "upstream provider exploded",
            "error_type": "ProviderError",
        },
    ]

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="half an answer please",
            sender_email=user["email"],
        ),
        chat,
        _LiveStream(events=events),
    )

    assert resp.status_code == 200
    notice_id = "spaces/AAA/messages/m1"

    # The half-answer was on screen before the failure was known.
    assert (notice_id, partial) in chat.updated, chat.updated

    # And the apology arrives beneath it, in the same message.
    assert chat.replaced == [
        (notice_id, f"{partial}\n\n{TURN_FAILED_TEXT}")
    ], chat.replaced
    # The provider's own words are never forwarded — they can carry internal
    # detail and the sender can act on none of it.
    assert "upstream provider exploded" not in chat.outbound_text
    assert chat.deleted == [], chat.deleted
    assert get_binding_status_message_id(db, channel["id"], thread_key) is None
