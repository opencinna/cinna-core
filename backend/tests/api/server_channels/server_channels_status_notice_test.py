"""The thread status notice — one message that narrates and then leaves.

What this replaces: a notice per pipeline state. "Got it — finding the right
assistant", "Setting up X for you", "Your assistant is ready" — three permanent
messages sitting above the one thing the person asked for, on every first
contact, forever.

Google Chat gives an app ``spaces.messages.patch`` over its **own** posts, so
the whole narration can instead be one message that is rewritten in place — and
whose *last* rewrite is the agent's own reply. The notice does not disappear
when the answer arrives; it becomes the answer.

That last part is a correction, and the reason it matters is visible to every
reader of the thread. The first version deleted the notice after posting the
reply beneath it, and Chat renders a deleted message as a **"Message deleted by
its author"** tombstone — so every single answer arrived under one. Reusing the
slot removes deletion from the common path entirely, which is also why
``supports_status_notice`` derives from edit alone;
``supports_message_delete`` gates only the two edges that end a turn with
nothing to say.

Three verbs:

* **set** — post it, or rewrite it. The id is kept on the binding.
* **settle** — rewrite it one last time and let go of the id, for a text that
  IS the answer ("nothing matched", "setup failed"). The message stays.
* **clear** — delete it. The exception, not the happy path: a stream that
  produced no message, and a routing race whose loser's notice has no thread
  left to narrate.

And the delivery itself, ``_deliver(into_status_notice=True)``, writes the
agent's reply into the slot via ``replace_message`` — chunked, unlike a notice
rewrite, since an agent's answer can be any length: the first chunk takes the
slot and the remainder is posted after it.

The consequence worth naming, because it is what the assertions below are
mostly about: an accepted message now acks the webhook in **silence**. The
narration moved out of the sync response because Chat creates that message but
never tells us its id — a notice answered synchronously can be neither
rewritten nor removed, which is precisely what this feature needs to do.

Markdown translation on the way out (`**bold**` → `*bold*`, which is why
`REPLY_INSTALLING` renders at all) happens *inside* the adapter, below the
mock used here; it is covered in `tests/unit/test_google_chat_format.py` and
`tests/unit/test_google_chat_sync_response.py`.
"""
import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.server_channels.adapters.base import (
    ChannelReplaceResult,
    ChannelSendError,
)
from app.services.server_channels.channel_inbound_service import (
    REPLY_NO_MATCH,
    REPLY_READY,
    REPLY_SETUP_FAILED,
    REPLY_WORKING,
    REPLY_WORKING_ON_IT,
)
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.environment import set_environment_status
from tests.utils.routing import refuse_to_classify
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_event,
    create_server_channel,
    flush_pending_bindings,
    get_binding_status_message_id,
    post_webhook,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

_ADAPTER = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter"
_SEND_TARGET = f"{_ADAPTER}.send_message"
_UPDATE_TARGET = f"{_ADAPTER}.update_message"
_REPLACE_TARGET = f"{_ADAPTER}.replace_message"
_DELETE_TARGET = f"{_ADAPTER}.delete_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"

# The id the mocked adapter hands back for every post. Real-shaped on purpose:
# the pipeline stores it in a varchar column and hands it straight back to
# `patch` and `delete` as a message resource name.
_NOTICE_ID = "spaces/AAA/messages/notice-1"


class _Chat:
    """The four outbound verbs, mocked together and read back by text."""

    def __init__(self) -> None:
        self.send = AsyncMock(return_value=_NOTICE_ID)
        self.update = AsyncMock(return_value=None)
        # A `ChannelReplaceResult`, not a bare id: `_deliver` reads `.replaced`
        # to decide whether the notice was really taken over, and releases the
        # binding's id only when it was. Returning a string here makes the
        # attribute read raise inside `_deliver`'s try, which the delivery path
        # cannot tell apart from a failed send.
        self.replace = AsyncMock(
            return_value=ChannelReplaceResult(message_id=_NOTICE_ID, replaced=True)
        )
        self.delete = AsyncMock(return_value=None)

    def apply(self, stack: ExitStack) -> "_Chat":
        stack.enter_context(patch(_SEND_TARGET, self.send))
        stack.enter_context(patch(_UPDATE_TARGET, self.update))
        stack.enter_context(patch(_REPLACE_TARGET, self.replace))
        stack.enter_context(patch(_DELETE_TARGET, self.delete))
        return self

    # `send_message(channel, thread_key, text)`;
    # `update_message(channel, thread_key, message_id, text)`;
    # `replace_message(channel, thread_key, message_id, text)`;
    # `delete_message(channel, thread_key, message_id)`.
    @property
    def sent(self) -> list[str]:
        return [c.args[-1] or "" for c in self.send.await_args_list]

    @property
    def updated(self) -> list[str]:
        return [c.args[-1] or "" for c in self.update.await_args_list]

    @property
    def replaced(self) -> list[tuple[str, str]]:
        """(message the text was written into, text)."""
        return [(c.args[2], c.args[3] or "") for c in self.replace.await_args_list]

    @property
    def deleted(self) -> list[str]:
        return [c.args[-1] for c in self.delete.await_args_list]

    @property
    def threads(self) -> set[str]:
        calls = (
            list(self.send.await_args_list)
            + list(self.update.await_args_list)
            + list(self.replace.await_args_list)
            + list(self.delete.await_args_list)
        )
        return {c.args[1] for c in calls}


#: A service-account blob, so the channel reads as having an outbound
#: credential. Load-bearing rather than decorative: the silent ack below is
#: gated on the channel actually being able to post the notice that replaces
#: it, and a Google Chat channel with an empty `encrypted_secrets` provably
#: cannot — that channel keeps the old synchronous `REPLY_WORKING` instead, so
#: an accepted sender is never answered with nothing at all.
_SECRETS = '{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "x"}'


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(
        auto_register_users=False, email_whitelist="*", secrets=_SECRETS
    )
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _sender_with_one_agent(client, superuser_headers, *, trigger: str | None = "Handle anything"):
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"Notice-{random_lower_string()[:6]}"
    )
    drain_tasks()
    if trigger is not None:
        set_router_trigger_prompt(client, headers, agent["id"], trigger)
    return user, headers, agent


def _post(client, channel, signer, event, chat: _Chat, stub) -> object:
    """One webhook delivery with the three outbound verbs under observation.

    The classifier is stubbed to RAISE: every scenario here is Pass 1's
    `only_one` short-circuit or a genuinely empty ballot, so reaching a model
    would mean the short-circuit stopped firing — and this file would rather
    say so than quietly call one.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, refuse_to_classify))
        chat.apply(stack)
        resp = post_webhook(
            client, channel["webhook_token"], event, bearer_token=token
        )
        drain_tasks()
    return resp


# ---------------------------------------------------------------------------
# The whole life of one notice
# ---------------------------------------------------------------------------


def test_a_new_thread_posts_one_notice_and_the_reply_takes_its_slot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Post → patch → *become the reply*. One bot message on the thread.

    Also the regression guard for the sync response: an accepted message must
    ack with an EMPTY body. Answering `REPLY_WORKING` there is what put the
    pipeline's first word outside the thread — Chat posts an unthreaded sync
    response into the space — and it is also unfixable in place, since Chat
    never returns that message's id.

    The silence is conditional on the channel having an outbound credential
    (`_SECRETS` above): without one the notice cannot be posted either, and
    answering nothing at all would leave an accepted sender with no reply from
    any surface.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    reply_text = "Here is the answer."

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        chat,
        StubAgentEnvConnector(response_text=reply_text),
    )

    assert resp.status_code == 200
    assert resp.json() == {}, resp.json()

    # Exactly ONE post for the whole exchange: the notice. Anything else would
    # mean a state posted a message instead of rewriting the one that exists.
    assert chat.sent == [REPLY_WORKING], chat.sent
    # The states in between are edits of that same message.
    assert chat.updated == [REPLY_WORKING_ON_IT], chat.updated
    assert [c.args[2] for c in chat.update.await_args_list] == [_NOTICE_ID]
    # And the answer lands IN it, rather than under it.
    assert chat.replaced == [(_NOTICE_ID, reply_text)], chat.replaced
    # Nothing is deleted, so no "Message deleted by its author" above the reply.
    assert chat.deleted == [], chat.deleted
    # Every call addressed to the thread the message arrived on.
    assert chat.threads == {thread_key}


def test_an_already_bound_thread_gets_its_own_notice_and_reuses_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The second turn, which used to be completely silent.

    A bound thread acked in silence and said nothing until the answer arrived,
    however long that took. It can say something now because the something
    turns into the answer — the thread ends up exactly as quiet as before, one
    bot message per turn.

    This also pins the invariant that makes the whole scheme safe: the notice
    id is **released** once the reply takes the slot. Were it not, the next
    turn's "working on your message…" would be patched straight over the
    previous answer and destroy it.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    first = _Chat()
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        first,
        StubAgentEnvConnector(response_text="first answer"),
    )
    assert first.replaced == [(_NOTICE_ID, "first answer")]
    assert first.deleted == []

    second = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="and again", sender_email=user["email"]
        ),
        second,
        StubAgentEnvConnector(response_text="second answer"),
    )

    assert resp.status_code == 200
    assert resp.json() == {}
    # No routing this time — the binding already names the agent — so the
    # notice opens straight at "working on it". It is a NEW message, which is
    # the released-id invariant observed from outside: had the first turn kept
    # its id, this would have been a patch over the previous answer.
    assert second.sent == [REPLY_WORKING_ON_IT], second.sent
    assert second.replaced == [(_NOTICE_ID, "second answer")], second.replaced
    assert second.deleted == [], second.deleted
    assert second.threads == {thread_key}


def test_no_match_settles_the_notice_instead_of_posting_under_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """"Nothing matched" IS the answer, so it takes the notice's place.

    And it is never deleted: settling releases the id precisely so that the
    last thing a thread was told survives.
    """
    user, headers = create_random_user_with_headers(client)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="do something", sender_email=user["email"]
        ),
        chat,
        StubAgentEnvConnector(response_text="never reached"),
    )

    assert resp.status_code == 200
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert chat.updated == [REPLY_NO_MATCH], chat.updated
    assert chat.replaced == [], chat.replaced
    assert chat.deleted == [], chat.deleted


def test_an_install_carries_the_notice_across_tasks_to_ready(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The hop the notice id lives on the binding for.

    The install announces itself minutes before the flush loop reports ready,
    from a different task with nothing but the binding row to go on. If the id
    were not adopted onto the binding at install time, "ready" would post a
    second message and the "setting up…" one would never be cleared.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(
        client, publisher_headers, name=f"NoticeBundle-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, publisher_headers, agent["id"], "Handle notice-flow requests"
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

    chat = _Chat()
    token = signer.token(audience=channel["config"]["project_number"])
    classify_result = types.SimpleNamespace(
        agent_id=bundle_uuid, transformed_message=None
    )
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=classify_result))
        chat.apply(stack)
        resp = post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=thread_key,
                text="install something for me",
                sender_email=consumer["email"],
            ),
            bearer_token=token,
        )
        drain_tasks()

    assert resp.status_code == 200
    # One post (the opening notice), then the install rewrote it in place.
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert len(chat.updated) == 1 and "Setting up" in chat.updated[0], chat.updated

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
    # "Ready" found the install's notice on the binding and rewrote it — no new
    # message — and the delivered answer then took its slot. So the whole
    # install story, minutes long and spanning two tasks, is ONE message in the
    # thread, and it ends up holding the answer.
    assert flush.updated == [REPLY_READY], flush.updated
    assert flush.replaced == [(_NOTICE_ID, "installed and answering")], flush.replaced
    assert flush.sent == [], flush.sent
    assert flush.deleted == [], flush.deleted


# ---------------------------------------------------------------------------
# The edges: nothing to say, delivery that fails, a notice that never posted
# ---------------------------------------------------------------------------


def test_a_stream_that_produced_nothing_deletes_the_notice(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`handle_stream_completed`'s OTHER branch — the one every other test in
    this file avoids by construction.

    Every scenario above ends with an agent message, so `chat.deleted == []`
    everywhere and `delete_message` / `supports_message_delete` /
    `clear_status`'s capability gate have no coverage at all. A stream that
    completes with no agent message at all is the genuine edge `clear`
    exists for: the notice would otherwise outlive the turn it was narrating
    and get rewritten by the NEXT one, telling the sender we are still
    working on a message from minutes ago.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    # No assistant event at all — `session_created`/`tools_init`/anything that
    # would leave an "agent" SessionMessage behind is deliberately absent, so
    # `_last_agent_message` finds nothing and `handle_stream_completed` takes
    # its `clear` branch instead of delivering into the notice.
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        chat,
        StubAgentEnvConnector(events=[{"type": "done"}]),
    )

    assert resp.status_code == 200
    assert resp.json() == {}
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert chat.updated == [REPLY_WORKING_ON_IT], chat.updated
    assert chat.replaced == [], chat.replaced
    # The notice — and only the notice — is deleted. No tombstone is left
    # standing over nothing, and nothing is posted in its place either.
    assert chat.deleted == [_NOTICE_ID], chat.deleted


def test_setup_failure_settles_and_releases_the_notice_id(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """`_flush_one`'s environment-failure path, asserted nowhere until now.

    `settle` is documented at length on `set_binding_status` — rewrite the
    notice one last time AND release the id — but nothing exercises it: this
    reaches it through the flush loop's own failure branch (the environment
    never becomes ready) rather than through `_route_new_thread`'s inline one.

    The release is proven, not merely asserted from the return value: the
    SAME agent is already installed for this sender, so a later message on
    the same thread self-heals (a `failed` binding is dropped and re-routed
    from scratch) and — because the id was actually let go of — that turn
    posts a BRAND NEW notice rather than patching "finding an assistant…"
    straight over the failure text the sender was just shown.
    """
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    agent = create_agent_via_api(
        client, publisher_headers, name=f"NoticeFail-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, publisher_headers, agent["id"], "Handle notice-failure requests"
    )
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    bundle_uuid = client.get(
        f"{API}/agents/{agent['id']}", headers=publisher_headers
    ).json()["bundle_uuid"]

    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    stub = StubAgentEnvConnector(response_text="never reached")

    chat = _Chat()
    token = signer.token(audience=channel["config"]["project_number"])
    classify_result = types.SimpleNamespace(
        agent_id=bundle_uuid, transformed_message=None
    )
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=classify_result))
        chat.apply(stack)
        resp = post_webhook(
            client,
            channel["webhook_token"],
            build_message_event(
                thread_key=thread_key,
                text="install something for me",
                sender_email=consumer["email"],
            ),
            bearer_token=token,
        )
        drain_tasks()

    assert resp.status_code == 200
    assert chat.sent == [REPLY_WORKING], chat.sent
    assert len(chat.updated) == 1 and "Setting up" in chat.updated[0], chat.updated

    installed = next(
        a
        for a in client.get(f"{API}/agents/", headers=consumer_headers).json()["data"]
        if a["bundle_uuid"] == bundle_uuid
    )
    # `error` is in `_ENV_FAILED` — terminal, not "still building" — so
    # `_flush_one` fails the binding on this very tick instead of waiting.
    set_environment_status(db, installed["active_environment_id"], "error")
    db.commit()

    flush = _Chat()
    with ExitStack() as stack:
        stack.enter_context(patch(_STREAM_TARGET, stub))
        flush.apply(stack)
        advanced = flush_pending_bindings(db)

    assert advanced == 0
    # Settled: the failure text takes the install notice's slot and stays —
    # a patch, not a fresh post, and no delivery (there was never a reply).
    assert flush.updated == [REPLY_SETUP_FAILED], flush.updated
    assert flush.sent == [], flush.sent
    assert flush.replaced == [], flush.replaced
    assert flush.deleted == [], flush.deleted

    # Recovery: fix the environment and send again. The failed binding
    # self-heals (deleted, re-routed from scratch); the sender already owns
    # the installed agent, so Pass 1 takes the `only_one` shortcut with no
    # install needed this time.
    set_environment_status(db, installed["active_environment_id"], "running")
    db.commit()

    second = _Chat()
    resp2 = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="try again", sender_email=consumer["email"]
        ),
        second,
        StubAgentEnvConnector(response_text="second attempt"),
    )

    assert resp2.status_code == 200
    # A brand-new notice — a SEND, not a PATCH of the failure message — is the
    # release proven from outside. Had the id survived on the (deleted, but
    # imagine it hadn't been) binding, or leaked anywhere else, this would
    # have patched "🔎 Finding the right assistant…" straight over "Sorry —
    # setting up your assistant failed…".
    assert second.sent == [REPLY_WORKING], second.sent
    assert second.replaced == [(_NOTICE_ID, "second attempt")], second.replaced


def test_delivery_failure_keeps_the_notice_id_for_the_next_turn(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`ChannelReplaceResult.replaced=False` — the "kept" half of the release
    contract, which every other test here only exercises the "released" half
    of.

    The patch fails (the adapter falls back to a plain post — exactly what a
    stale notice id looks like in production), so the notice is still
    standing, still saying "working on your message…", with the reply posted
    underneath it. The id must survive on the binding: releasing it here would
    orphan that message, since nothing would own it to rewrite or delete.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    first = _Chat()
    first.replace = AsyncMock(
        return_value=ChannelReplaceResult(
            message_id="spaces/AAA/messages/fallback", replaced=False
        )
    )
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        first,
        StubAgentEnvConnector(response_text="first answer"),
    )
    # The reply went out addressed at the notice's slot (the pipeline always
    # tries), but the adapter reports it did NOT actually land there.
    assert first.replaced == [(_NOTICE_ID, "first answer")], first.replaced

    second = _Chat()
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="and again", sender_email=user["email"]
        ),
        second,
        StubAgentEnvConnector(response_text="second answer"),
    )

    assert resp.status_code == 200
    # No fresh notice posted — the SAME message (still standing, still
    # "working on your message…" from before) is patched again. Compare
    # `test_an_already_bound_thread_gets_its_own_notice_and_reuses_it`, where
    # a REAL replacement makes this a `send` instead.
    assert second.sent == [], second.sent
    assert second.updated == [REPLY_WORKING_ON_IT], second.updated
    assert second.update.await_args.args[2] == _NOTICE_ID
    assert second.replaced == [(_NOTICE_ID, "second answer")], second.replaced


def test_a_notice_that_could_not_be_posted_still_lets_the_answer_through(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A failed OPENING notice degrades to no acknowledgement, never to no
    answer.

    `_send_notice` swallows the failure and hands `_route_new_thread` back a
    `None` id. Routing and delivery proceed regardless — there is simply
    nothing to adopt onto the binding, so the eventual reply goes out as an
    ordinary `send_message` (there is no notice to write it into) rather than
    being silently dropped along with the notice.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    # First call (the opening notice) fails; the second (the eventual reply,
    # posted plainly since there is no slot to replace into) succeeds.
    chat.send = AsyncMock(
        side_effect=[ChannelSendError("boom"), "spaces/AAA/messages/answer-1"]
    )
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        chat,
        StubAgentEnvConnector(response_text="the answer"),
    )

    assert resp.status_code == 200
    assert resp.json() == {}
    # Nothing was ever adopted onto the binding, so nothing was patched and
    # nothing was replaced — the reply is a plain send, twice over.
    assert chat.updated == [], chat.updated
    assert chat.replaced == [], chat.replaced
    assert chat.deleted == [], chat.deleted
    assert chat.send.call_count == 2
    assert chat.send.call_args_list[0].args[-1] == REPLY_WORKING
    assert chat.send.call_args_list[1].args[-1] == "the answer"


def test_outbound_credentials_gate_the_synchronous_ack(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The silent `{}` ack requires BOTH capability and something to post
    the notice with.

    Without an outbound credential the notice provably cannot be posted, so
    the channel keeps the old synchronous `REPLY_WORKING` acknowledgement —
    the sender is never left with literally nothing. With one, the ack is
    silent, as pinned by
    `test_a_new_thread_posts_one_notice_and_the_reply_takes_its_slot` above;
    reasserted here as the direct contrast.

    `tests/utils/server_channel.py::create_server_channel` defaults to
    `secrets=None`, and `server_channels_email_test.py` depends on that
    default staying put — so both channels here pass `secrets` explicitly
    rather than relying on it.
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    uncredentialed = _channel(client, superuser_token_headers, secrets=None)
    thread_a = f"spaces/AAA/threads/{random_lower_string()}"
    chat_a = _Chat()
    resp_a = _post(
        client,
        uncredentialed,
        signer,
        build_message_event(
            thread_key=thread_a, text="hello", sender_email=user["email"]
        ),
        chat_a,
        StubAgentEnvConnector(response_text="answer a"),
    )
    assert resp_a.status_code == 200
    assert resp_a.json() == {"text": REPLY_WORKING, "thread": {"name": thread_a}}
    # The gate only changes what the SYNCHRONOUS ack says — it does not
    # suppress the background task's own attempt to post the notice.
    assert chat_a.sent == [REPLY_WORKING], chat_a.sent

    credentialed = _channel(client, superuser_token_headers)  # _SECRETS default
    thread_b = f"spaces/AAA/threads/{random_lower_string()}"
    chat_b = _Chat()
    resp_b = _post(
        client,
        credentialed,
        signer,
        build_message_event(
            thread_key=thread_b, text="hello", sender_email=user["email"]
        ),
        chat_b,
        StubAgentEnvConnector(response_text="answer b"),
    )
    assert resp_b.status_code == 200
    assert resp_b.json() == {}


def test_the_notice_id_is_cleared_on_the_binding_between_turns(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The release, read directly off the row — the invariant this whole
    scheme rests on, pinned as its own fact rather than only inferred from a
    second turn's behaviour (as the other tests in this file do).
    """
    user, headers, agent = _sender_with_one_agent(client, superuser_token_headers)
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"

    chat = _Chat()
    _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key, text="hello", sender_email=user["email"]
        ),
        chat,
        StubAgentEnvConnector(response_text="the answer"),
    )
    assert chat.replaced == [(_NOTICE_ID, "the answer")], chat.replaced

    assert get_binding_status_message_id(db, channel["id"], thread_key) is None
