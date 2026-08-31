"""A soft ingest failure must never look like a delivery.

``ChannelIngestionService.ingest_inbound_message`` reports a whole family of
failures — session vanished, agent has no active environment, environment
activation failed, file preparation refused the message — as a **soft**
``action="error"`` result rather than as an exception. The web-UI chat route
wants that; it renders the friendly text. On a channel it was a trap.

``ChannelInboundService._continue_thread`` decided whether to stamp
``binding.last_external_message_id`` on *"did an exception escape
``_ingest_or_fail``"*, never on *"was a message actually created"*. So a soft
error stamped the binding exactly as a delivered message would, and the
sender's message was gone: no message in the transcript, no error reply, no
debug-feed entry, and no second chance either — the next redelivery of that
same external message dedups against a delivery that never happened.

**This is not an attachment bug.** It was found through one (a reused
``FileUpload`` row arriving at ``prepare_user_message_with_files`` in
``"attached"`` state), but the shape is general: ``_continue_thread`` commits
the message and stamps the binding in two *separate* commits, so a crash
between them leaves the same state with no attachment anywhere in the story.
That is why it is pinned here, on a plain text message, rather than only
inside the attachment suite.

The fix: ``_ingest`` raises ``ChannelIngestProducedNoMessage`` on
``action="error"``, ``_ingest_or_fail`` returns whether a message was created
and publishes the outcome to the debug feed, and the stamp is gated on that
answer. The failure becomes an ordinary visible one — failed binding, generic
setup-failed reply, self-heal and re-route on the next message — which is the
correct disposition even though it is *louder* than the silence it replaces.
"""
from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import ChannelThreadBinding
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages
from tests.utils.routing import enter_classifier_patch
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    build_message_event,
    create_server_channel,
    post_webhook,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = (
    "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
)
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_SEND_MESSAGE_TARGET = (
    "app.services.sessions.session_service.SessionService.send_session_message"
)


def _sender_with_agent(client: TestClient, superuser_headers: dict[str, str]):
    """A platform user owning exactly one eligible agent (``only_one`` routing)."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"SoftFailAgent-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _post(client, channel, signer, event):
    """One verified Chat webhook delivery, drained."""
    token = signer.token(audience=channel["config"]["project_number"])
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(
            patch(_STREAM_TARGET, StubAgentEnvConnector(response_text="ok"))
        )
        stack.enter_context(patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")))
        enter_classifier_patch(stack)
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp


def _binding(db: Session, channel_id: str) -> ChannelThreadBinding | None:
    db.expire_all()
    return db.exec(
        select(ChannelThreadBinding).where(
            ChannelThreadBinding.server_channel_id == uuid.UUID(channel_id)
        )
    ).first()


def test_a_soft_ingest_error_neither_stamps_the_binding_nor_vanishes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The retry door must stay open when nothing was delivered.

    First message routes and binds normally. The second is met by a soft
    ``action="error"`` from the ingestion layer — the shape a missing
    environment, a failed activation or a refused file preparation produces —
    and must:

      1. create no second user message (it genuinely failed);
      2. leave ``binding.last_external_message_id`` on the FIRST message's id,
         so a redelivery of the second is still processed rather than deduped
         away against a delivery that never happened;
      3. fail the binding, which is the platform's existing self-heal signal
         (the next inbound message deletes it and re-routes).

    (2) is the assertion that used to fail, and it is the one that turns a
    recoverable failure into permanent loss when it does.
    """
    channel = create_server_channel(
        client, superuser_token_headers, auto_register_users=False, email_whitelist="*"
    )
    signer = GoogleChatJWTSigner()
    user, headers, agent = _sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    first_id = f"spaces/AAA/messages/{random_lower_string()}"
    resp = _post(
        client,
        channel,
        signer,
        build_message_event(
            thread_key=thread_key,
            text="first message",
            sender_email=user["email"],
            message_name=first_id,
        ),
    )
    assert resp.status_code == 200

    binding = _binding(db, channel["id"])
    assert binding is not None
    assert binding.last_external_message_id == first_id
    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    session_id = sessions[0]["id"]

    # ---- The second message hits a soft error --------------------------
    second_id = f"spaces/AAA/messages/{random_lower_string()}"
    with patch(
        _SEND_MESSAGE_TARGET,
        AsyncMock(return_value={"action": "error", "message": "environment is gone"}),
    ):
        resp2 = _post(
            client,
            channel,
            signer,
            build_message_event(
                thread_key=thread_key,
                text="second message",
                sender_email=user["email"],
                message_name=second_id,
            ),
        )
    assert resp2.status_code == 200

    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs) == 1, (
        "the soft error created no message; anything else here means the "
        "patch did not take"
    )

    binding = _binding(db, channel["id"])
    assert binding is not None
    assert binding.last_external_message_id == first_id, (
        "a delivery that produced no message must not be stamped — the stamp "
        "is what dedups every later redelivery, so stamping it here loses the "
        "sender's message permanently and silently"
    )
    assert binding.status == "failed", (
        "the failure must be visible and self-healing, not swallowed"
    )
