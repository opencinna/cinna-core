"""The attachment-rejection notice on a polled transport, now that its gate
is reachable.

``ChannelInboundService.process_inbound``'s all-attachments-rejected branch
used to be **dead code on email**, the one transport it was written for. Its
predicate was ``not inbound.text.strip()``, and email's ``inbound.text`` is
``EmailPollingService.format_email_as_message(...)`` — which emits
``--- Forwarded email content ---`` and a ``From:`` line for every mail,
including one whose subject and body are both empty. So the predicate was
false for every email ever polled: an attachment-only mail whose every
attachment was refused ingested "successfully" as the wrapper text plus a ⚠️
note, created a session, woke an agent, and told the sender nothing.

The fix carries the emptiness **beside** the text rather than parsing it back
out of a formatted string: ``EmailChannelAdapter._to_inbound_message``
declares ``ChannelInboundMessage.sender_text_empty`` from the subject and body
it has in hand before any wrapping, and the pipeline reads
``inbound.has_sender_text``. The wrapper itself cannot be made conditional —
it is the only route by which a subject reaches a channel-routed agent.

This file covers the properties that became testable the moment that branch
could fire, and which nothing else pins:

  - the notice is really sent, end to end, over the real polled path;
  - it is suppressed by the platform-wide outbound-email gate
    (``EmailConfirmationService.is_outbound_email_allowed``) exactly as every
    other outbound mail is;
  - it is total — an SMTP failure costs the notice and never the poll tick,
    which matters because earlier messages in that tick are already ``\\Seen``
    on the IMAP server and are never re-fetched;
  - the gate does **not** over-fire: a blank-bodied mail whose attachment was
    *accepted* is an ordinary message and must still reach an agent.

``server_channels_attachments_test.py`` owns the rest of the feature; this is
the notice seam only.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.utils import generate_email_confirmation_token
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.email_stubs import StubIMAPConnector, StubSMTPConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.email_channel import (
    IMAP_CONNECTOR_TARGET,
    SMTP_CONNECTOR_TARGET,
    build_raw_email_with_attachments,
    create_email_channel,
    poll_channel,
)
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"

_MAILBOX = "support@corp.example"


@pytest.fixture(autouse=True)
def _patch_upload_base_path(tmp_path):
    """Attachment bytes are written for real; give them a tmp disk root.

    Same fixture, and the same reason, as
    ``server_channels_attachments_test.py``.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        yield


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _sender_with_agent(client: TestClient, superuser_headers: dict[str, str]):
    """A platform user owning exactly one eligible agent.

    One agent means Pass 1's ``only_one`` short-circuit answers, so none of
    these tests needs a classifier stub.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(
        client, headers, name=f"NoticeAgent-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _confirm_email(client: TestClient, email: str) -> None:
    """Confirm a signed-up user's address through the real public route.

    Signup leaves ``email_confirmed=False`` (``UserService`` auto-confirms
    superusers only), and every outbound mail on the platform is gated on that
    column. A test that wants to observe a *send* has to clear the gate the
    way a person does, not by writing the column.
    """
    token = generate_email_confirmation_token(email=email)
    r = client.post(f"{API}/confirm-email/", json={"token": token})
    assert r.status_code == 200, r.text


def _email_channel(client: TestClient, superuser_headers: dict[str, str]) -> dict:
    imap = create_imap_server(client, superuser_headers)
    smtp = create_smtp_server(client, superuser_headers)
    return create_email_channel(
        client,
        superuser_headers,
        incoming_server_id=imap["id"],
        outgoing_server_id=smtp["id"],
        incoming_mailbox=_MAILBOX,
        email_whitelist="*",
    )


def _attachment_only_mail(sender_email: str, *, filename: str, mime_type: str) -> str:
    """A mail with no subject and no body — its whole content is one file."""
    return build_raw_email_with_attachments(
        message_id=f"<{random_lower_string()}@sender.example>",
        sender=sender_email,
        to=_MAILBOX,
        subject="",
        body="",
        attachments=[
            {
                "filename": filename,
                "content": b"MZ-fake-binary" if filename.endswith(".exe") else b"%PDF-1.4 x",
                "mime_type": mime_type,
            }
        ],
    )


def _poll(db: Session, raw: str, smtp_stub: StubSMTPConnector) -> int:
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        SMTP_CONNECTOR_TARGET, smtp_stub
    ), patch(_STREAM_TARGET, StubAgentEnvConnector(response_text="ok")):
        processed = poll_channel(db)
        drain_tasks()
    return processed


# ---------------------------------------------------------------------------
# 1. The branch is reachable, and the notice really goes out
# ---------------------------------------------------------------------------


def test_the_rejection_notice_reaches_a_confirmed_sender_and_never_routes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The whole point of the fix, observed end to end on the polled path.

    A mail with an empty subject, an empty body and one refused attachment is
    answered by exactly one mail, addressed to the **resolved platform
    account** rather than to the ``From:`` header (which is spoofable on this
    transport), and never reaches routing at all — there is nothing to route
    on and nothing to hand an agent.

    Before the fix this produced zero mails and one live session: the gate was
    unreachable because ``format_email_as_message`` had already made
    ``inbound.text`` non-empty.
    """
    _email_channel(client, superuser_token_headers)
    user, headers, agent = _sender_with_agent(client, superuser_token_headers)
    _confirm_email(client, user["email"])

    smtp_stub = StubSMTPConnector()
    processed = _poll(
        db,
        _attachment_only_mail(
            user["email"], filename="app.exe", mime_type="application/x-msdownload"
        ),
        smtp_stub,
    )
    assert processed == 1

    assert len(smtp_stub.sent_emails) == 1, (
        "the sender's whole message was a refused attachment; expected exactly "
        f"one rejection notice, got {smtp_stub.sent_emails!r}"
    )
    sent = smtp_stub.sent_emails[0]
    assert sent["to"] == user["email"], (
        "the notice must go to the RESOLVED platform account's own address, "
        "never a value re-derived from the (spoofable) From: header"
    )
    body = "".join(
        part.get_payload(decode=True).decode("utf-8", "replace")
        for part in sent["msg"].walk()
        if part.get_content_maintype() == "text"
    )
    assert "app.exe" in body, f"the notice must name what was refused: {body!r}"

    assert not any(s["agent_id"] == agent["id"] for s in list_sessions(client, headers)), (
        "a message whose entire content was refused must never reach routing"
    )


# ---------------------------------------------------------------------------
# 2. The outbound-email gate still applies to it
# ---------------------------------------------------------------------------


def test_the_rejection_notice_is_suppressed_for_an_unconfirmed_sender(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The notice is not exempt from the platform's outbound-email gate.

    ``EmailConfirmationService.is_outbound_email_allowed`` is the single check
    every non-recovery outbound mail passes (``NotificationService``,
    ``EmailSendingService._send_single_email``, and this notice), and it exists
    because filenames are attacker-supplied text: without it, spoofing
    ``From: victim@example.com`` at a whitelisted channel would relay a short
    attacker-authored string into an inbox that never confirmed it belongs to
    a platform account.

    So an unconfirmed sender gets silence here — the same silence their
    agent's replies would get from the queue's own copy of this gate. This was
    previously untestable: the branch could not fire at all.
    """
    _email_channel(client, superuser_token_headers)
    user, headers, agent = _sender_with_agent(client, superuser_token_headers)
    # Deliberately NOT confirmed.

    smtp_stub = StubSMTPConnector()
    processed = _poll(
        db,
        _attachment_only_mail(
            user["email"], filename="app.exe", mime_type="application/x-msdownload"
        ),
        smtp_stub,
    )
    assert processed == 1
    assert smtp_stub.sent_emails == [], (
        "outbound mail to an unconfirmed address is gated platform-wide; the "
        f"notice must not be the one exception: {smtp_stub.sent_emails!r}"
    )
    # Suppressing the notice must not resurrect the message either.
    assert not any(s["agent_id"] == agent["id"] for s in list_sessions(client, headers))


# ---------------------------------------------------------------------------
# 3. Totality — the notice never costs the tick
# ---------------------------------------------------------------------------


def test_an_smtp_failure_costs_the_notice_and_never_the_poll_tick(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A raising SMTP connector must not abandon the poll tick.

    Load-bearing rather than tidy: by the time this runs, earlier messages in
    the same tick are already marked ``\\Seen`` on the IMAP server and will
    never be re-fetched, so an exception escaping here would lose real mail to
    tell somebody about one refused attachment.

    Also previously untestable — the code path could not be entered.
    """
    _email_channel(client, superuser_token_headers)
    user, headers, agent = _sender_with_agent(client, superuser_token_headers)
    _confirm_email(client, user["email"])

    class _ExplodingSMTP(StubSMTPConnector):
        def send(self, *args, **kwargs):  # noqa: ANN002, ANN003 - stub shape
            raise RuntimeError("smtp is down")

    processed = _poll(
        db,
        _attachment_only_mail(
            user["email"], filename="app.exe", mime_type="application/x-msdownload"
        ),
        _ExplodingSMTP(),
    )
    assert processed == 1, "the tick must complete even though the notice failed"
    assert not any(s["agent_id"] == agent["id"] for s in list_sessions(client, headers)), (
        "the message is still declined — a failed notice does not resurrect it"
    )


# ---------------------------------------------------------------------------
# 4. The gate must not over-fire
# ---------------------------------------------------------------------------


def test_a_blank_bodied_mail_with_an_accepted_attachment_still_reaches_an_agent(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``sender_text_empty`` widens what the decline *can* see, not what it does.

    The same mail shape as the tests above — empty subject, empty body, one
    attachment — but the attachment is accepted. That is an ordinary
    attachment-only message: it routes, it reaches an agent, and it produces
    no notice. A fix that keyed the decline on "the sender wrote nothing"
    alone, rather than on "the sender wrote nothing **and** nothing survived",
    would swallow this.
    """
    _email_channel(client, superuser_token_headers)
    user, headers, agent = _sender_with_agent(client, superuser_token_headers)
    _confirm_email(client, user["email"])

    smtp_stub = StubSMTPConnector()
    processed = _poll(
        db,
        _attachment_only_mail(
            user["email"], filename="brief.pdf", mime_type="application/pdf"
        ),
        smtp_stub,
    )
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1, "an accepted attachment-only mail is a real message"
    assert smtp_stub.sent_emails == [], (
        "nothing was refused, so there is nothing to apologise for: "
        f"{smtp_stub.sent_emails!r}"
    )
