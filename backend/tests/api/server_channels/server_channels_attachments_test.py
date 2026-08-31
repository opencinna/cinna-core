"""Channel Message Attachments — plan §12 checklist
(docs/drafts/channel-message-attachments_plan.md).

Inbound file attachments for Server Channels (Google Chat + Email), reaching
the agent exactly the way a web-UI file upload does. Read the domain
``README.md`` first — in particular "No binding read API" and "Whether you
must name a classifier answer depends on the *catalog*".

Covers:
  - A verified Google Chat message with one attachment -> a ``FileUpload``
    owned by the sender + a ``MessageFile`` on the user message.
  - A ``DRIVE_FILE`` attachment is never fetched.
  - Webhook redelivery of a message with an attachment creates no second
    ``FileUpload`` — the safe case (dedup at step 3 stops the second
    delivery before step 6.5 ever runs). NOT the "redelivery inside the
    dedup window on an already-bound thread" case, which is a currently
    known-imperfect edge (see the test's own docstring).
  - An attachment over the per-file cap is skipped; the message still
    reaches the agent, and the stored content names the file and the reason.
  - Attachment-only message, all skipped -> ``REPLY_ATTACHMENTS_REJECTED``.
  - Attachment-only message, accepted -> routes on a filename-derived
    classification string (requires an actual classifier call, so the
    sender owns two eligible agents here — see the domain README's
    "only_one" note).
  - Email: a MIME attachment produces a ``FileUpload``; an inline
    ``cid:``-referenced part does not; ``EmailMessage.attachments_metadata``
    still carries no bytes.
  - Parking: a Pass-2 auto-install parks a message with an attachment and the
    drain delivers the *same* file. A parked entry with no ``file_ids`` key
    (the pre-deploy shape) drains without error.
  - **The regression this feature shipped with**: an attachment-only parked
    message (``text == ""``) used to be silently dropped by
    ``_drain_parked``'s old ``if text:`` gate — no error, no reply, no
    transcript line, nothing in the debug feed. Fixed to ``if text or
    file_ids:``; pinned here so it stays fixed.
  - Identity routing: the sender's attachment lands in the identity owner's
    session, owned by the SENDER, and is charged against the SENDER's own
    storage quota — not the owner's (plan §3.4, the subtlest ownership
    property in the feature).
  - Quota exhaustion, MIME rejection and the per-message count cap each
    produce a skip and never a 5xx.

NOT covered here (see file-level notes at each cross-reference):
  - The media fetch's URL construction, egress-guard call, and the
    resourceName shape-validation ("../scheme refused before any request")
    are pure, I/O-free properties of ``GoogleChatAdapter._media_url`` and are
    unit-tested in ``tests/unit/test_google_chat_fetch_attachment.py``
    (cross-reference convention, ``tests/README.md``) rather than re-proven
    here through a full webhook round trip.
  - Every emittable skip reason rendering to sender-safe prose with no
    underscore-token is unit-tested in
    ``tests/unit/test_channel_reason_phrase.py`` (pure function, no I/O).
"""
from __future__ import annotations

import io
import types
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import settings
from app.models import ChannelThreadBinding
from app.models.email.email_message import EmailMessage
from app.utils import generate_email_confirmation_token
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.email_stubs import StubIMAPConnector, StubSMTPConnector
from tests.utils.agent import create_agent_via_api, set_router_trigger_prompt
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle_and_make_public
from tests.utils.email_channel import (
    IMAP_CONNECTOR_TARGET,
    SMTP_CONNECTOR_TARGET,
    build_forwarded_message,
    build_raw_email,
    build_raw_email_with_attachments,
    build_raw_email_with_forward,
    create_email_channel,
    poll_channel,
)
from tests.utils.environment import set_environment_status
from tests.utils.identity import share_identity_agent
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.message import list_messages
from tests.utils.routing import classification, enter_classifier_patch
from tests.utils.server_channel import (
    GoogleChatJWTSigner,
    add_auto_install_bundle,
    build_message_attachment,
    build_message_event,
    create_server_channel,
    flush_pending_bindings,
    post_webhook,
)
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_channel import update_my_channel
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_SEND_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.send_message"
_STREAM_TARGET = "app.services.sessions.message_service.agent_env_connector"
_FETCH_TARGET = "app.services.server_channels.adapters.google_chat.GoogleChatAdapter.fetch_attachment"
_CLASSIFY_TARGET = "app.services.routing.agent_classifier.AgentClassifier.classify"
_STORE_FILE_TARGET = "app.services.files.file_storage_service.FileStorageService.store_file"
_EXTRACT_ATTACHMENTS_TARGET = (
    "app.services.email.polling_service.EmailPollingService._extract_attachments"
)


# ---------------------------------------------------------------------------
# Module-scoped fixture — real bytes need a real (tmp) disk root
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_upload_base_path(tmp_path):
    """Every test in this file materialises real attachment bytes to disk.

    Mirrors the per-test ``patch.object(settings, "UPLOAD_BASE_PATH", ...)``
    every other attachment-materialising test in the suite uses (see
    ``agents_message_attachments_test.py``, ``test_a2a_inbound_file_
    attachments.py``) — centralised here as a file-scoped autouse fixture
    since nearly every test in this file needs it.
    """
    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        yield


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _channel(client, superuser_headers, **overrides) -> dict:
    defaults = dict(auto_register_users=False, email_whitelist="*")
    defaults.update(overrides)
    return create_server_channel(client, superuser_headers, **defaults)


def _known_sender_with_agent(client, superuser_headers):
    """A platform user with exactly one eligible agent — Pass 1's `only_one`
    short-circuit, so no classifier answer is ever needed for these tests."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers, name=f"AttachAgent-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent["id"], "Handle anything")
    return user, headers, agent


def _confirm_email(client, email: str) -> None:
    """Confirm a signed-up user's address through the real public route.

    Signup leaves ``email_confirmed=False`` (``UserService`` auto-confirms
    superusers only), and every outbound mail on the platform — including
    ``EmailChannelAdapter.send_rejection_notice`` — is gated on that column
    via ``EmailConfirmationService.is_outbound_email_allowed``. A test that
    wants to observe a *send* has to clear the gate the way a person does,
    not by writing the column. Mirrors
    ``server_channels_rejection_notice_test.py::_confirm_email``.
    """
    token = generate_email_confirmation_token(email=email)
    r = client.post(f"{API}/confirm-email/", json={"token": token})
    assert r.status_code == 200, r.text


def _post_chat(
    client,
    channel,
    signer,
    event,
    *,
    stub=None,
    classify_result=None,
    fetch_return_value: bytes = b"",
    fetch_side_effect=None,
):
    """One verified Chat webhook delivery, drained.

    Always patches ``fetch_attachment`` (harmless when the event carries no
    attachments) so every test in this file can pass attachment content
    through one call, mirroring how ``server_channels_webhook_test.py``'s
    ``_post`` always patches ``send_message``. The classifier is patched via
    ``enter_classifier_patch``, which installs a raising refusal stub when no
    answer is named — every test here that names none is relying on Pass 1's
    `only_one` short-circuit and wants a loud failure if that ever stops
    firing, not a silent call to a real (blocked) provider.
    """
    token = signer.token(audience=channel["config"]["project_number"])
    stream_stub = stub or StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stream_stub))
        send_mock = stack.enter_context(
            patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id"))
        )
        if fetch_side_effect is not None:
            fetch_mock = stack.enter_context(
                patch(_FETCH_TARGET, AsyncMock(side_effect=fetch_side_effect))
            )
        else:
            fetch_mock = stack.enter_context(
                patch(_FETCH_TARGET, AsyncMock(return_value=fetch_return_value))
            )
        enter_classifier_patch(stack, classify_result=classify_result)
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()
    return resp, send_mock, fetch_mock


def _binding_for_channel(db: Session, channel_id: str) -> ChannelThreadBinding | None:
    db.expire_all()
    return db.exec(
        select(ChannelThreadBinding).where(
            ChannelThreadBinding.server_channel_id == uuid.UUID(channel_id)
        )
    ).first()


def _user_message(client, headers, session_id: str) -> dict:
    msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(msgs) == 1, msgs
    return msgs[0]


def _mail_servers(client, superuser_headers) -> tuple[str, str]:
    imap = create_imap_server(client, superuser_headers)
    smtp = create_smtp_server(client, superuser_headers)
    return imap["id"], smtp["id"]


def _download(client: TestClient, headers: dict[str, str], file_id: str):
    return client.get(f"{API}/files/{file_id}/download", headers=headers)


def _resolve_original(target: str):
    """Resolve the real callable a ``patch(target)`` string names, without a
    static ``import app.services...`` anywhere in this file.

    Walks the string with ``importlib`` (trying the longest importable module
    prefix first) and then ``getattr``s the remaining attribute chain off the
    resulting module/class — the normal descriptor-protocol resolution, which
    matters for a ``@staticmethod``: a raw ``__dict__`` lookup can hand back
    an unwrapped ``staticmethod`` object rather than a callable, depending on
    the Python version, and ``getattr`` on the live class does not.
    """
    import importlib

    parts = target.split(".")
    module = None
    split_at = len(parts)
    for split_at in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split_at]))
            break
        except ImportError:
            continue
    if module is None:  # pragma: no cover - defensive, not expected to trip
        raise RuntimeError(f"Could not import any prefix of {target!r}")
    obj = module
    for part in parts[split_at:-1]:
        obj = getattr(obj, part)
    return getattr(obj, parts[-1])


def _spy(target: str):
    """Patch ``target`` with a Mock that calls through to the real
    implementation — a spy, not a replacement, so a test can assert *how many
    times* a real effect happened without disabling it.

    Returns ``(patcher, mock)``; the caller is responsible for
    ``patcher.stop()``.
    """
    original = _resolve_original(target)
    patcher = patch(target)
    mock = patcher.start()
    mock.side_effect = original
    return patcher, mock


def _setup_pending_install(
    client,
    superuser_headers,
    *,
    text: str = "park me please",
    attachment: dict | None = None,
    fetch_return_value: bytes = b"",
) -> dict:
    """Consumer messages a channel, matches Pass 2, and parks — before the
    environment is ready. Mirrors ``server_channels_pending_outbound_test.py
    ::_setup_pending_install``, extended with an optional attachment so the
    same helper serves the plain-text and attachment-carrying park scenarios.
    """
    # ``make_user_and_headers`` (not ``create_random_user_with_headers``): both
    # the publisher AND the consumer need a default AI credential — the
    # consumer's is what lets ``InstallService.install_bundle`` provision the
    # environment for the copy installed onto THEIR account. Missing it here
    # doesn't fail loudly; it just means the consumer never ends up with an
    # agent carrying this bundle_uuid, which is a confusing StopIteration two
    # network calls later rather than an obvious credential error.
    consumer, consumer_headers = make_user_and_headers(client)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_headers, publisher["id"])
    agent = create_agent_via_api(
        client, publisher_headers, name=f"AttPending-{random_lower_string()[:6]}"
    )
    drain_tasks()
    set_router_trigger_prompt(
        client, publisher_headers, agent["id"], "Handle pending-attachment test requests"
    )
    publish_bundle_and_make_public(client, publisher_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher_headers).json()
    bundle_uuid = fresh["bundle_uuid"]

    channel = _channel(client, superuser_headers)
    signer = GoogleChatJWTSigner()
    add_auto_install_bundle(client, superuser_headers, bundle_uuid)

    classify_result = types.SimpleNamespace(agent_id=bundle_uuid, transformed_message=None)
    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text=text,
        sender_email=consumer["email"],
        attachments=[attachment] if attachment is not None else None,
    )
    token = signer.token(audience=channel["config"]["project_number"])
    stub = StubAgentEnvConnector(response_text="Sure thing.")

    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")))
        stack.enter_context(patch(_CLASSIFY_TARGET, return_value=classify_result))
        if attachment is not None:
            stack.enter_context(
                patch(_FETCH_TARGET, AsyncMock(return_value=fetch_return_value))
            )
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
        "channel": channel,
        "thread_key": thread_key,
    }


# ---------------------------------------------------------------------------
# 1. Basic materialisation + ownership
# ---------------------------------------------------------------------------


def test_verified_chat_message_with_attachment_produces_sender_owned_file(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A verified Google Chat message carrying one attachment:
      1. Produces a ``FileUpload`` + ``MessageFile`` on the stored user
         message, projected as ``source="user_upload"`` — indistinguishable
         from a web upload downstream.
      2. The file is owned by the SENDER: the sender can download it.
      3. A completely unrelated third user cannot.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)
    _stranger, stranger_headers = create_random_user_with_headers(client)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    content = b"%PDF-1.4 fake pdf content"
    event = build_message_event(
        thread_key=thread_key,
        text="please review this",
        sender_email=user["email"],
        attachments=[build_message_attachment(content_name="report.pdf", content_type="application/pdf")],
    )

    resp, _send_mock, fetch_mock = _post_chat(
        client, channel, signer, event, fetch_return_value=content
    )
    assert resp.status_code == 200
    fetch_mock.assert_awaited_once()

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1, sessions
    session_id = sessions[0]["id"]

    message = _user_message(client, headers, session_id)
    assert "please review this" in (message["content"] or "")
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "report.pdf"
    assert files[0]["mime_type"] == "application/pdf"
    assert files[0]["source"] == "user_upload"
    file_id = files[0]["id"]

    # Owned by the sender.
    assert _download(client, headers, file_id).status_code == 200
    # A completely unrelated user has neither the owner arm nor the
    # session-participant arm of check_download_permission.
    assert _download(client, stranger_headers, file_id).status_code == 403


# ---------------------------------------------------------------------------
# 2. DRIVE_FILE is never fetched
# ---------------------------------------------------------------------------


def test_drive_file_attachment_is_never_fetched(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A Drive attachment is reported (unavailable_reason="drive_file") but
    ``fetch_attachment`` is never called for it — the app has no Drive
    credential and no consent to act on one (plan §4.2)."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text="check this doc",
        sender_email=user["email"],
        attachments=[build_message_attachment(content_name="shared-doc.gdoc", drive_file=True)],
    )

    resp, _send_mock, fetch_mock = _post_chat(client, channel, signer, event)
    assert resp.status_code == 200
    fetch_mock.assert_not_awaited()

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    session_id = sessions[0]["id"]
    message = _user_message(client, headers, session_id)
    assert message["files"] == []
    content = message["content"] or ""
    assert "shared-doc.gdoc" in content
    assert "Google Drive" in content


# ---------------------------------------------------------------------------
# 3. Redelivery creates no second file
# ---------------------------------------------------------------------------


def test_redelivery_of_a_message_with_an_attachment_creates_no_second_file(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A webhook redelivery of the same message (same ``external_message_id``)
    on an ALREADY-BOUND, ACTIVE thread is caught by ``binding.
    last_external_message_id`` at step 3 — before step 6.5 ever runs — so the
    attachment is neither re-fetched nor re-stored.

    This is the SAFE-BY-CONSTRUCTION case: this test's second delivery
    arrives strictly after the first one's ``drain_tasks()`` completed and
    stamped the id, which the step-3 dedup catches on its own, before
    ``ChannelAttachmentService.materialize`` is ever called a second time.

    The OTHER window — a redelivery that reaches step 6.5 a second time
    because the stamp was never written (the first ingest failed downstream
    of materialisation) — used to be able to materialise files twice. It is
    now closed by ``ChannelAttachmentService``'s own idempotency, keyed on
    ``(server_channel_id, thread_key, external_message_id,
    attachment_index)`` and stored in the existing ``file_metadata`` JSON (no
    migration). That window — the one this test's docstring used to describe
    as "currently imperfect" — is exercised directly in
    ``test_a_genuine_step_6_5_retry_reuses_survivors_and_only_re_materialises_
    what_is_missing`` below, by resetting ``binding.last_external_message_id``
    to simulate the stamp never having been written.

    Asserted on the ``FileStorageService.store_file`` **call count**, not
    only the resulting ``FileUpload``/``files`` count — a future
    materialisation that stores twice and deduplicates the rows afterwards
    would still show one row here, but not one store call. A real bytes
    write is spied on (not replaced) so this stays a true end-to-end proof.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    message_name = f"spaces/AAA/messages/{random_lower_string()}"
    content = b"attachment bytes for dedup test"
    event = build_message_event(
        thread_key=thread_key,
        text="first delivery",
        sender_email=user["email"],
        message_name=message_name,
        attachments=[build_message_attachment()],
    )

    store_patcher, store_mock = _spy(_STORE_FILE_TARGET)
    try:
        resp1, _send1, fetch1 = _post_chat(
            client, channel, signer, event, fetch_return_value=content
        )
        assert resp1.status_code == 200
        fetch1.assert_awaited_once()
        assert store_mock.call_count == 1

        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1
        session_id = sessions[0]["id"]
        assert len(_user_message(client, headers, session_id)["files"]) == 1

        # Redelivery of the SAME event.
        resp2, _send2, fetch2 = _post_chat(
            client, channel, signer, event, fetch_return_value=content
        )
        assert resp2.status_code == 200
        assert resp2.json() == {}
        fetch2.assert_not_awaited()
        assert store_mock.call_count == 1, "store_file was called again on redelivery"
    finally:
        store_patcher.stop()

    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs) == 1, "Redelivered message must not be ingested twice"
    assert len(user_msgs[0]["files"]) == 1, "No second FileUpload from the redelivery"


def test_a_genuine_step_6_5_retry_reuses_survivors_and_only_re_materialises_what_is_missing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    The OTHER redelivery window — the one the previous test's docstring used
    to describe as "currently imperfect": a retry that reaches step 6.5 a
    SECOND time because ``binding.last_external_message_id`` was never
    stamped by the first ingest (a downstream ingest failure, after
    materialisation but before the stamp). Simulated directly here — the
    same documented DB-seam exemption ``test_parked_entry_with_no_file_ids_
    key_drains_without_error`` uses to reach a state no API call can
    reproduce — because reproducing a genuine downstream failure is not
    needed to prove the file-level idempotency this closes.

    The message carries three attachments: ``good1.pdf`` and ``good2.pdf``
    (accepted) and ``toobig.pdf`` (skipped, over the per-file cap, on BOTH
    deliveries — the cap does not change between them). Between the two
    deliveries, ``good1.pdf``'s row is soft-deleted through the real
    ``DELETE /files/{id}`` endpoint — ``_existing_materializations`` excludes
    ``marked_for_deletion`` rows by design (those bytes are already promised
    back to the owner), so this simulates "the row genuinely isn't there any
    more" through an ordinary API call rather than a direct DB mutation.

    On the retry:
      1. ``good2.pdf`` is REUSED — no fetch, no store — proving idempotency is
         per attachment position, not all-or-nothing for the message.
      2. ``good1.pdf`` IS re-fetched and re-stored, under a NEW file id — the
         missing row is finished, not silently left absent.
      3. ``toobig.pdf`` is fetched again and skipped again for the same
         reason, and named again in the second message's content — a
         previously-skipped attachment does not silently vanish on a retry
         just because it was already reported once.
      4. File order in the second message follows ref order (good1, good2),
         not "reused first."

    **FINDING (currently RED — reported, not worked around; see the runner's
    instructions):** points 1-2 above (the ``fetch2`` / ``store_mock``
    assertions) pass — ``ChannelAttachmentService``'s own idempotency lookup
    and per-position skip-index plumbing work exactly as designed. But the
    test as a whole fails at the "two user messages" assertion afterward,
    because of a real interaction bug one layer up:

    ``good2.pdf`` was already attached to the FIRST message, and
    ``MessageService.prepare_user_message_with_files`` flips a file's
    ``status`` from ``"temporary"`` to ``"attached"`` the moment it is used
    (``FileService.mark_files_as_attached``). On the retry,
    ``ChannelAttachmentService`` correctly REUSES ``good2.pdf``'s row (no
    re-fetch, no re-store — the assertions above prove it) and hands its id
    down to ``prepare_user_message_with_files`` for the SECOND message — which
    rejects it: ``if file.status != "temporary": raise MessageServiceError(
    "File already attached: ...")``. That exception does not propagate as a
    failure a sender or admin can see: ``ChannelIngestionService.
    ingest_inbound_message`` returns it as a soft ``action="error"`` result
    rather than raising, so ``ChannelInboundService._ingest`` does not raise
    either, and ``_continue_thread`` — which decides whether to stamp
    ``binding.last_external_message_id`` purely on "did an exception escape
    ``_ingest_or_fail``", not on whether the ingest actually produced a
    message — stamps the binding as if the retry had succeeded. Net effect:
    the sender's retried message is silently and PERMANENTLY lost. No second
    message, no error reply, no debug-feed entry, and no further retry
    opportunity, because the next identical redelivery is now deduped as
    already-processed.

    This is a real gap in the idempotency design (plan §A), not a flaw in
    this test's setup: the one documented trigger for reaching step 6.5 a
    second time — "the first ingest failed downstream of materialisation, so
    the stamp was never written" — is exactly the shape needed to hit it
    whenever that first, unstamped attempt nonetheless got as far as
    attaching at least one file to a message before whatever crashed it (a
    failure between the message-creation commit and the later, separate
    binding-stamp commit, which are two distinct commits in
    ``_continue_thread``). Per this project's testing rules, this is reported
    rather than fixed or routed around: do not soften these assertions, and
    do not modify ``app/`` to make this test pass.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    message_name = f"spaces/AAA/messages/{random_lower_string()}"
    contents = {
        "good1.pdf": b"%PDF-1.4 good one",
        "toobig.pdf": b"x" * (2 * 1024 * 1024),
        "good2.pdf": b"%PDF-1.4 good two",
    }

    def _fetch(_channel_arg, ref):
        return contents[ref.filename]

    event = build_message_event(
        thread_key=thread_key,
        text="three attachments, one too big",
        sender_email=user["email"],
        message_name=message_name,
        attachments=[
            build_message_attachment(content_name="good1.pdf", content_type="application/pdf"),
            build_message_attachment(content_name="toobig.pdf", content_type="application/pdf"),
            build_message_attachment(content_name="good2.pdf", content_type="application/pdf"),
        ],
    )

    store_patcher, store_mock = _spy(_STORE_FILE_TARGET)
    try:
        with patch.object(settings, "CHANNEL_ATTACHMENT_MAX_FILE_MB", 1):
            resp1, _send1, fetch1 = _post_chat(
                client, channel, signer, event, fetch_side_effect=_fetch
            )
        assert resp1.status_code == 200
        assert fetch1.await_count == 3
        assert store_mock.call_count == 2, "good1.pdf and good2.pdf should each store once"

        sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
        assert len(sessions) == 1
        session_id = sessions[0]["id"]
        message1 = _user_message(client, headers, session_id)
        files1 = message1["files"]
        assert [f["filename"] for f in files1] == ["good1.pdf", "good2.pdf"]
        assert "toobig.pdf" in (message1["content"] or "")
        good1_id_v1 = files1[0]["id"]
        good2_id = files1[1]["id"]

        # Soft-delete good1's row through the real API.
        r_delete = client.delete(f"{API}/files/{good1_id_v1}", headers=headers)
        assert r_delete.status_code == 200, r_delete.text

        # Simulate the stamp never having been written by the first ingest —
        # the only way to reach step 6.5 a second time for the SAME
        # external_message_id.
        binding = _binding_for_channel(db, channel["id"])
        assert binding is not None
        binding.last_external_message_id = None
        db.add(binding)
        db.commit()

        with patch.object(settings, "CHANNEL_ATTACHMENT_MAX_FILE_MB", 1):
            resp2, _send2, fetch2 = _post_chat(
                client, channel, signer, event, fetch_side_effect=_fetch
            )
        assert resp2.status_code == 200
        # good1.pdf (deleted) and toobig.pdf (never stored) are re-fetched;
        # good2.pdf (still a live row) is not — this much is proven and
        # green: ``ChannelAttachmentService``'s own idempotency lookup and
        # skip-index plumbing behave exactly as designed.
        assert fetch2.await_count == 2
        assert store_mock.call_count == 3, (
            "only good1.pdf's re-store, on top of the two stores from the "
            "first delivery"
        )
    finally:
        store_patcher.stop()

    # ---- This is where the test currently goes red — see the docstring's
    # "FINDING" section. `ChannelIngestionService.ingest_inbound_message`
    # returns `action="error"` for this retry (`prepare_user_message_with_
    # files` raises `MessageServiceError("File already attached: good2.pdf")`
    # because good2.pdf's `FileUpload.status` was flipped from "temporary" to
    # "attached" when the FIRST message consumed it), `_ingest` does not
    # raise, and `_continue_thread` stamps `binding.last_external_message_id`
    # anyway — indistinguishable from a genuine success. The sender's retried
    # message is silently and permanently lost: no second message, no error
    # reply, no debug-feed entry, and no further retry opportunity (the next
    # identical redelivery is now deduped as already-processed).
    user_msgs = [m for m in list_messages(client, headers, session_id) if m["role"] == "user"]
    assert len(user_msgs) == 2, "the retry is a genuine second ingest (the stamp was reset)"
    message2 = user_msgs[1]
    files2 = message2["files"]
    assert [f["filename"] for f in files2] == ["good1.pdf", "good2.pdf"], (
        "file order must follow ref order, not reused-first"
    )
    assert files2[0]["id"] != good1_id_v1, "good1.pdf must be a NEW row, not the deleted one"
    assert files2[1]["id"] == good2_id, "good2.pdf must be the SAME row, reused"
    assert "toobig.pdf" in (message2["content"] or ""), (
        "a previously-skipped attachment must be named again on a retry, "
        "not silently dropped because it was already reported once"
    )


# ---------------------------------------------------------------------------
# 4. Over the per-file cap
# ---------------------------------------------------------------------------


def test_attachment_over_the_per_file_cap_is_skipped_but_message_still_reaches_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    big_content = b"x" * (2 * 1024 * 1024)
    event = build_message_event(
        thread_key=thread_key,
        text="here is a big file",
        sender_email=user["email"],
        attachments=[build_message_attachment(content_name="huge.pdf", content_type="application/pdf")],
    )

    with patch.object(settings, "CHANNEL_ATTACHMENT_MAX_FILE_MB", 1):
        resp, _send_mock, fetch_mock = _post_chat(
            client, channel, signer, event, fetch_return_value=big_content
        )
    assert resp.status_code == 200
    fetch_mock.assert_awaited_once()

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    session_id = sessions[0]["id"]
    message = _user_message(client, headers, session_id)
    assert message["files"] == []
    text = message["content"] or ""
    assert "here is a big file" in text
    assert "huge.pdf" in text
    assert "exceeds the 1MB limit" in text


# ---------------------------------------------------------------------------
# 5 + 6. Attachment-only: all skipped vs. accepted
# ---------------------------------------------------------------------------


def test_attachment_only_message_all_skipped_gets_the_explicit_rejection_reply(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An attachment-only message where every attachment is refused is
    declined with the specific ``REPLY_ATTACHMENTS_REJECTED`` text — this is
    the one decline that is deliberately NOT the generic indistinguishable
    reply (plan §4.5) — and never reaches routing at all."""
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text="",
        sender_email=user["email"],
        attachments=[
            build_message_attachment(content_name="app.exe", content_type="application/x-msdownload")
        ],
    )

    resp, _send_mock, fetch_mock = _post_chat(
        client, channel, signer, event, fetch_return_value=b"MZ-fake-binary"
    )
    assert resp.status_code == 200
    body_text = resp.json().get("text", "")
    assert "couldn't accept" in body_text, body_text
    assert "app.exe" in body_text
    assert "file type isn't supported" in body_text

    # Declined before routing: no session was ever created for this thread.
    assert not any(s["agent_id"] == agent["id"] for s in list_sessions(client, headers))


def test_attachment_only_message_accepted_routes_on_filename_derived_text(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    An attachment-only message with its (only) attachment accepted routes on
    a filename-derived classification string, per plan §5.4 — the router
    never sees an empty string.

    Requires the sender to own TWO eligible agents (per the domain README:
    naming a classifier answer is only needed, and only meaningful, when
    Pass 1's `only_one` short-circuit cannot fire), so the classifier is
    genuinely invoked and its ``message`` argument can be captured.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    agent1 = create_agent_via_api(client, headers, name=f"AttA-{random_lower_string()[:6]}")
    drain_tasks()
    agent2 = create_agent_via_api(client, headers, name=f"AttB-{random_lower_string()[:6]}")
    drain_tasks()
    set_router_trigger_prompt(client, headers, agent1["id"], "Handle anything one")
    set_router_trigger_prompt(client, headers, agent2["id"], "Handle anything two")

    captured_messages: list[str] = []

    def _classify(candidates, message, **_kw):
        captured_messages.append(message)
        return classification(agent1["id"])

    thread_key = f"spaces/AAA/threads/{random_lower_string()}"
    event = build_message_event(
        thread_key=thread_key,
        text="",
        sender_email=user["email"],
        attachments=[build_message_attachment(content_name="report.pdf", content_type="application/pdf")],
    )

    token = signer.token(audience=channel["config"]["project_number"])
    stub = StubAgentEnvConnector(response_text="ok")
    with ExitStack() as stack:
        stack.enter_context(signer.patched())
        stack.enter_context(patch(_STREAM_TARGET, stub))
        stack.enter_context(patch(_SEND_TARGET, AsyncMock(return_value="fake-ext-id")))
        stack.enter_context(patch(_FETCH_TARGET, AsyncMock(return_value=b"%PDF-1.4 fake")))
        stack.enter_context(patch(_CLASSIFY_TARGET, side_effect=_classify))
        resp = post_webhook(client, channel["webhook_token"], event, bearer_token=token)
        drain_tasks()

    assert resp.status_code == 200
    assert captured_messages == ["(sent 1 file: report.pdf)"], captured_messages

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent1["id"]]
    assert len(sessions) == 1, sessions
    message = _user_message(client, headers, sessions[0]["id"])
    assert len(message["files"]) == 1


# ---------------------------------------------------------------------------
# 7. Email: MIME attachment + inline cid: exclusion
# ---------------------------------------------------------------------------


def test_email_mime_attachment_produces_a_file_upload(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_attachments(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="Please see attached",
        body="find the report attached",
        attachments=[
            {"filename": "report.pdf", "content": b"%PDF-1.4 fake report", "mime_type": "application/pdf"}
        ],
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="Got it")
    ):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    message = _user_message(client, headers, sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "report.pdf"
    assert files[0]["mime_type"] == "application/pdf"
    assert files[0]["source"] == "user_upload"


def test_email_inline_cid_part_is_excluded_and_metadata_carries_no_bytes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A part disposed ``attachment`` but referenced via ``cid:`` in the HTML
    body is excluded from both the agent's attachments AND the durable
    ``EmailMessage.attachments_metadata`` — the extraction loop never adds it
    to the returned list at all (plan §5.6). ``attachments_metadata`` is read
    directly off the row, per this domain's own precedent
    (``server_channels_email_test.py``'s module docstring): there is no
    admin/user-facing GET for it."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_attachments(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="With a logo",
        body="see the report",
        html_body='<p>see the report</p><img src="cid:logo123">',
        attachments=[
            {
                "filename": "logo.png",
                "content": b"\x89PNGfakebytes",
                "mime_type": "image/png",
                "content_id": "logo123",
            },
            {"filename": "report.pdf", "content": b"%PDF-1.4 fake report", "mime_type": "application/pdf"},
        ],
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    message = _user_message(client, headers, sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "report.pdf"
    assert all(f["filename"] != "logo.png" for f in files)

    db.expire_all()
    row = db.exec(select(EmailMessage).where(EmailMessage.email_message_id == msg_id)).first()
    assert row is not None
    metadata = row.attachments_metadata or []
    assert len(metadata) == 1, metadata
    assert metadata[0]["filename"] == "report.pdf"
    assert "content" not in metadata[0]


def test_a_malformed_attachment_part_in_one_message_does_not_wedge_the_whole_poll_tick(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Regression for a real incident found while building this feature:
    ``_parse_email``'s only guard used to be around
    ``email.message_from_bytes`` — everything after it, including attachment
    extraction, ran unprotected. A raise there propagated out of
    ``_parse_email``, through the adapter's blocking mailbox fetch, and
    ultimately turned the whole poll tick into nothing at all — and the
    damage is not "one bad message skipped": the blocking fetch marks each
    accepted message ``\\Seen`` as it goes, *before* returning the list, so
    every message accepted earlier in the same tick is already marked read
    and then discarded along with the tick when the later one raises. The
    offending mail stays unread and fails identically on the next tick — the
    mailbox becomes permanently undrainable.

    Both layers of the fix are exercised at once here (a per-part guard
    inside ``_extract_attachments`` and a whole-extraction guard around it in
    ``_parse_email``): this test only asserts the OBSERVABLE property that
    matters from the outside — a real poll tick with three messages, the
    middle one's extraction failing, must still deliver all three, in one
    call that never raises.

    The failure is injected via a spy on ``EmailPollingService.
    _extract_attachments`` that raises only for the one message identified by
    its Message-ID and calls through to the real implementation for the
    other two — the real MIME/decoding stdlib has become defensive enough
    across Python versions that reliably reproducing a genuine raise from
    hand-crafted malformed bytes alone is not stable across environments;
    injecting the exact failure class (an unhandled exception mid-extraction)
    at the real call boundary the guard wraps is what the guard's own
    contract is written against, and is functionally identical to what a
    non-canonical MIME edge case would trigger for real.

    Ordering is the crux, per the plan owner: the failing message sits in the
    MIDDLE, with an accepted message both before and after it in the same
    IMAP fetch — a single-message tick cannot distinguish the fixed code from
    the wedging code, since a broken tick and a one-message tick both
    "deliver zero or one message".
    """
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    before_id = f"<before-{random_lower_string()}@sender.example>"
    malformed_id = f"<malformed-{random_lower_string()}@sender.example>"
    after_id = f"<after-{random_lower_string()}@sender.example>"

    raw_before = build_raw_email(
        message_id=before_id, sender=user["email"], to=mailbox,
        subject="Before", body="the message before the malformed one",
    )
    raw_malformed = build_raw_email_with_attachments(
        message_id=malformed_id,
        sender=user["email"],
        to=mailbox,
        subject="Malformed",
        body="this message has a malformed attachment part",
        attachments=[
            {"filename": "broken.bin", "content": b"whatever bytes", "mime_type": "application/octet-stream"}
        ],
    )
    raw_after = build_raw_email(
        message_id=after_id, sender=user["email"], to=mailbox,
        subject="After", body="the message right after the malformed one",
    )

    real_extract_attachments = _resolve_original(_EXTRACT_ATTACHMENTS_TARGET)

    def _flaky_extract_attachments(msg, **kwargs):
        if (msg.get("Message-ID") or "").strip() == malformed_id:
            raise RuntimeError(
                "simulated: a malformed MIME attachment part that raises "
                "during extraction"
            )
        return real_extract_attachments(msg, **kwargs)

    with patch(_EXTRACT_ATTACHMENTS_TARGET, side_effect=_flaky_extract_attachments), patch(
        IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw_before, raw_malformed, raw_after])
    ), patch(_STREAM_TARGET, StubAgentEnvConnector(response_text="ok")):
        processed = poll_channel(db)  # must not raise
        drain_tasks()

    assert processed == 3, (
        "the whole tick was abandoned because one message's attachment "
        "extraction raised — the exact wedge this test guards against"
    )

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 3, sessions

    def _message_containing(marker: str) -> dict | None:
        for session in sessions:
            msgs = [m for m in list_messages(client, headers, session["id"]) if m["role"] == "user"]
            if msgs and marker in (msgs[0]["content"] or ""):
                return msgs[0]
        return None

    before_msg = _message_containing("the message before the malformed one")
    after_msg = _message_containing("the message right after the malformed one")
    malformed_msg = _message_containing("this message has a malformed attachment part")

    assert before_msg is not None, (
        "the message BEFORE the malformed one was lost — already-\\Seen mail "
        "discarded along with the rest of the tick"
    )
    assert after_msg is not None, (
        "the message AFTER the malformed one was lost — proves the tick "
        "kept going past the failure rather than aborting"
    )
    assert malformed_msg is not None, (
        "the malformed message itself disappeared instead of degrading "
        "gracefully"
    )
    assert malformed_msg["files"] == [], (
        "a message whose attachment extraction raised must arrive with no "
        "attachments, never a partially-materialised one"
    )


# ---------------------------------------------------------------------------
# 7a. Forwarded email (`message/rfc822`): loose parts win, `.eml` is fallback
#     only (plan §5.6). This INVERTS an earlier ruling in this feature's own
#     history — a forward's own inner parts arriving loose was, at one point,
#     double-counted alongside a `.eml` of the container; it is now strictly
#     either/or, and these three tests are the first coverage of any of it.
# ---------------------------------------------------------------------------


def test_forwarded_email_with_a_loose_attachment_delivers_only_that_file(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A forward carrying ``invoice.pdf`` as a loose part inside the
    ``message/rfc822`` container delivers ``invoice.pdf`` ONLY. The container
    itself is never a failed attachment — it produces no entry, no skip and
    no sender-visible notice, exactly as if the forward wrapper did not
    exist."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    forwarded = build_forwarded_message(
        subject="Q3 Invoice",
        sender="vendor@example.com",
        body="please remit payment",
        attachments=[
            {
                "filename": "invoice.pdf",
                "content": b"%PDF-1.4 invoice",
                "mime_type": "application/pdf",
            }
        ],
    )
    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_forward(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="Fwd: Q3 Invoice",
        body="fyi, see attached",
        forwarded=forwarded,
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    message = _user_message(client, headers, sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "invoice.pdf"
    assert files[0]["mime_type"] == "application/pdf"
    content = message["content"] or ""
    assert "forwarded-message.eml" not in content, (
        "the message/rfc822 container must produce no entry of its own when "
        "its inner parts arrived loose"
    )
    assert "could not be accepted" not in content


def test_text_only_forward_with_nothing_deliverable_inside_falls_back_to_the_eml(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A forward whose inner message carries no deliverable part — just text,
    no attachment — falls back to serialising the container itself as one
    ``.eml`` attachment, rather than being silently lost the way it was
    before this feature (``get_payload(decode=True)`` returns ``None`` for a
    ``message/rfc822`` container, which used to surface as ``size=0`` /
    ``no_content``)."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    forwarded = build_forwarded_message(
        subject="Just some words",
        sender="colleague@example.com",
        body="nothing attached, only text",
    )
    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_forward(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="Fwd: Just some words",
        body="fyi",
        forwarded=forwarded,
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    message = _user_message(client, headers, sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "forwarded-message.eml"
    assert files[0]["mime_type"] == "message/rfc822"


def test_nested_forward_produces_exactly_one_file(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A forward of a forward: the INNERMOST container is processed first
    (plan §5.6, "innermost first") and contributes the one ``.eml`` the whole
    chain delivers; the outer container then sees its own subtree already
    produced something and contributes nothing — never two files for one
    forwarded chain."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    innermost = build_forwarded_message(
        subject="Original",
        sender="original@example.com",
        body="the original message",
    )
    middle = build_forwarded_message(
        subject="Fwd: Original",
        sender="middle@example.com",
        body="fwd note",
        nested_forward=innermost,
    )
    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_forward(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="Fwd: Fwd: Original",
        body="fwd of a fwd",
        forwarded=middle,
    )
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        _STREAM_TARGET, StubAgentEnvConnector(response_text="ok")
    ):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    message = _user_message(client, headers, sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["filename"] == "forwarded-message.eml"


# ---------------------------------------------------------------------------
# 7b. Email rejection notice (new seam: ChannelAdapter.send_rejection_notice)
# ---------------------------------------------------------------------------


def test_email_rejection_notice_is_never_sent_when_the_message_ingests_fine(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The notice fires from exactly one place — the whole-message
    attachment-rejection branch — and must never fire on a message that
    ingested successfully, even though one attachment arrived fine. A notice
    that fired here would double up with the agent's own reply, on the one
    transport (email) that has no way to distinguish "a decline" from "an
    answer" except by which mail arrives."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_attachments(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="See attached",
        body="here is the file",
        attachments=[
            {"filename": "report.pdf", "content": b"%PDF-1.4 x", "mime_type": "application/pdf"}
        ],
    )
    smtp_stub = StubSMTPConnector()
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        SMTP_CONNECTOR_TARGET, smtp_stub
    ), patch(_STREAM_TARGET, StubAgentEnvConnector(response_text="ok")):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    sessions = [s for s in list_sessions(client, headers) if s["agent_id"] == agent["id"]]
    assert len(sessions) == 1
    assert smtp_stub.sent_emails == [], (
        "the attachment-rejection notice fired on a message that ingested "
        f"successfully: {smtp_stub.sent_emails!r}"
    )


def test_email_attachment_only_rejection_notifies_the_resolved_sender_by_email(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """An email whose entire content is a refused attachment (no usable text)
    is answered by a mailed notice — the one sender-facing text this
    transport sends outside an agent's own reply
    (``EmailChannelAdapter.send_rejection_notice``), because a polled
    transport's declines are otherwise silent by design.

    The recipient must be the RESOLVED platform account's own address — never
    re-derived from the ``From:`` header the message arrived with, which is
    spoofable on this transport (module docstring, ``adapters/email.py``).

    The sender must be a CONFIRMED account: the notice send passes through
    the same platform-wide ``EmailConfirmationService.is_outbound_email_
    allowed`` gate as every other outbound mail (``NotificationService``,
    ``EmailSendingService._send_single_email``), and ``create_random_user_
    with_headers`` signup leaves ``email_confirmed=False``. That gate must
    not be weakened for this seam: filenames are attacker-supplied text, and
    without it, spoofing ``From:`` at a whitelisted channel would relay a
    short attacker-authored string into an inbox that never confirmed it
    belongs to a platform account."""
    imap_id, smtp_id = _mail_servers(client, superuser_token_headers)
    mailbox = "support@corp.example"
    create_email_channel(
        client,
        superuser_token_headers,
        incoming_server_id=imap_id,
        outgoing_server_id=smtp_id,
        incoming_mailbox=mailbox,
        email_whitelist="*",
    )
    user, headers, agent = _known_sender_with_agent(client, superuser_token_headers)
    _confirm_email(client, user["email"])

    msg_id = f"<{random_lower_string()}@sender.example>"
    raw = build_raw_email_with_attachments(
        message_id=msg_id,
        sender=user["email"],
        to=mailbox,
        subject="",
        body="",
        attachments=[
            {
                "filename": "app.exe",
                "content": b"MZ-fake-binary",
                "mime_type": "application/x-msdownload",
            }
        ],
    )
    smtp_stub = StubSMTPConnector()
    with patch(IMAP_CONNECTOR_TARGET, StubIMAPConnector(emails=[raw])), patch(
        SMTP_CONNECTOR_TARGET, smtp_stub
    ), patch(_STREAM_TARGET, StubAgentEnvConnector(response_text="ok")):
        processed = poll_channel(db)
        drain_tasks()
    assert processed == 1

    assert len(smtp_stub.sent_emails) == 1, (
        "the sender's entire message was a refused attachment with no usable "
        f"text; expected exactly one rejection notice, got {smtp_stub.sent_emails!r}"
    )
    sent = smtp_stub.sent_emails[0]
    assert sent["to"] == user["email"], (
        "the rejection notice must go to the RESOLVED platform account's own "
        "address, never a value re-derived from the (spoofable) From: header"
    )
    assert not any(s["agent_id"] == agent["id"] for s in list_sessions(client, headers)), (
        "a message whose entire content was refused must never reach routing"
    )


# ---------------------------------------------------------------------------
# 8, 9, 10. Parking
# ---------------------------------------------------------------------------


def test_pass2_auto_install_parks_attachment_and_drain_delivers_the_same_file(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    content = b"%PDF-1.4 pending install content"
    setup = _setup_pending_install(
        client,
        superuser_token_headers,
        text="please review when ready",
        attachment=build_message_attachment(content_name="brief.pdf", content_type="application/pdf"),
        fetch_return_value=content,
    )

    binding = _binding_for_channel(db, setup["channel"]["id"])
    assert binding is not None
    parked_file_ids = binding.pending_messages[0].get("file_ids") or []
    assert len(parked_file_ids) == 1, binding.pending_messages

    set_environment_status(db, setup["env_id"], "running")
    db.commit()
    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ):
        advanced = flush_pending_bindings(db)
        drain_tasks()
    assert advanced == 1

    sessions = [
        s for s in list_sessions(client, setup["consumer_headers"])
        if s["agent_id"] == setup["installed_agent"]["id"]
    ]
    assert len(sessions) == 1
    message = _user_message(client, setup["consumer_headers"], sessions[0]["id"])
    files = message["files"]
    assert len(files) == 1, files
    assert files[0]["id"] == parked_file_ids[0], "The drained file_ids must match what was parked"


def test_parked_entry_with_no_file_ids_key_drains_without_error(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """§3.2's pre-deploy compatibility case: a binding parked BEFORE this
    feature shipped has no ``file_ids`` key in its ``pending_messages`` entry
    at all. No API path can produce that shape any more — every entry this
    deploy writes carries the key, even when empty — so it is simulated
    directly on the row, the same documented-exemption pattern
    ``server_channels_email_test.py::test_binding_thread_key_is_total_and_a_
    deleted_binding_declines_the_send`` uses to simulate a state no API call
    can reach."""
    setup = _setup_pending_install(client, superuser_token_headers, text="an old-shape park")
    binding = _binding_for_channel(db, setup["channel"]["id"])
    assert binding is not None
    entries = list(binding.pending_messages or [])
    assert entries, "precondition: a message must already be parked"
    entries[0] = {k: v for k, v in entries[0].items() if k != "file_ids"}
    binding.pending_messages = entries
    flag_modified(binding, "pending_messages")
    db.add(binding)
    db.commit()

    set_environment_status(db, setup["env_id"], "running")
    db.commit()
    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ):
        advanced = flush_pending_bindings(db)
        drain_tasks()
    assert advanced == 1

    sessions = [
        s for s in list_sessions(client, setup["consumer_headers"])
        if s["agent_id"] == setup["installed_agent"]["id"]
    ]
    assert len(sessions) == 1
    message = _user_message(client, setup["consumer_headers"], sessions[0]["id"])
    assert message["files"] == []
    assert "an old-shape park" in (message["content"] or "")


def test_attachment_only_parked_message_is_not_silently_dropped_on_drain(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Pins the regression this feature shipped with: ``_drain_parked``'s old
    ``if text:`` gate silently ATE an attachment-only parked entry.

    An accepted-attachments-no-text message parks with ``text == ""`` (the
    composed text is only the skip note, and there is none — every
    attachment here is accepted). The old gate treated an empty ``text`` as
    "nothing to deliver" and skipped the entry without ever calling
    ``_ingest`` — no exception, no failed binding, no reply, no line in the
    session transcript, and nothing in the admin debug feed to explain where
    the sender's file went. It is fixed to ``if text or file_ids:``.

    If this test ever goes red, it means that gate regressed back to
    ``if text:`` (or equivalent) and a sender's attachment-only message is
    being silently discarded during a Pass-2 install park — the exact
    failure mode this test exists to catch before it reaches production.
    """
    content = b"%PDF-1.4 no words just files"
    setup = _setup_pending_install(
        client,
        superuser_token_headers,
        text="",
        attachment=build_message_attachment(content_name="brief.pdf", content_type="application/pdf"),
        fetch_return_value=content,
    )

    binding = _binding_for_channel(db, setup["channel"]["id"])
    assert binding is not None
    parked = binding.pending_messages[0]
    assert parked["text"] == "", (
        "precondition: an accepted-attachments-no-text message must park "
        f"with an empty text, not {parked['text']!r}"
    )
    assert len(parked.get("file_ids") or []) == 1

    set_environment_status(db, setup["env_id"], "running")
    db.commit()
    with patch(_STREAM_TARGET, setup["stub"]), patch(
        _SEND_TARGET, AsyncMock(return_value="fake-ext-id")
    ):
        advanced = flush_pending_bindings(db)
        drain_tasks()
    assert advanced == 1

    sessions = [
        s for s in list_sessions(client, setup["consumer_headers"])
        if s["agent_id"] == setup["installed_agent"]["id"]
    ]
    assert len(sessions) == 1, (
        "An attachment-only parked message (empty text) was dropped by "
        "_drain_parked instead of being delivered — the sender's file was "
        "silently eaten. This is the `if text or file_ids:` regression."
    )
    message = _user_message(client, setup["consumer_headers"], sessions[0]["id"])
    assert len(message["files"]) == 1


# ---------------------------------------------------------------------------
# 11. Identity routing: sender-owned file, owner's session, sender's quota
# ---------------------------------------------------------------------------


def test_identity_routed_attachment_is_sender_owned_and_charges_the_senders_quota(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The subtlest ownership property in the feature (plan §3.4): on an
    identity-routed thread, ``session.user_id`` is the OWNER but the
    attachment's ``FileUpload.user_id`` must be the SENDER.

      1. The session lands in the owner's workspace, on the owner's agent.
      2. The file is owned by the sender: the sender can download it
         directly (the file-owner arm of ``check_download_permission``), and
         so can the owner (the session-participant arm) — a third party can
         do neither.
      3. The sender's OWN storage quota is what gets charged, not the
         owner's: the owner's quota is deliberately filled first via an
         ordinary web upload, leaving no room for the attachment if it were
         (incorrectly) charged there — and the attachment is still accepted,
         because it is charged against the sender's own (empty) usage.
    """
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    owner, owner_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner["id"])
    create_random_ai_credential(client, owner_headers, set_default=True)
    hr_agent = create_agent_via_api(client, owner_headers, name=f"HRAtt-{random_lower_string()[:6]}")
    drain_tasks()

    sender, sender_headers = create_random_user_with_headers(client)
    share_identity_agent(
        client,
        owner_headers,
        sender_headers,
        agent_id=hr_agent["id"],
        target_user_id=sender["id"],
        owner_id=owner["id"],
        trigger_prompt="Answer HR questions about leave.",
        prompt_examples="what is my leave balance",
    )
    settings_row = update_my_channel(
        client, sender_headers, channel["id"], allow_identity_routing=True
    )
    assert settings_row["allow_identity_routing"] is True, settings_row

    max_bytes = 5000
    owner_upload_size = 4600
    sender_attachment_size = 900  # owner_used + this > max_bytes; 0 + this < max_bytes

    with patch.object(settings, "UPLOAD_MAX_USER_STORAGE_GB", max_bytes / (1024**3)):
        r_upload = client.post(
            f"{API}/files/upload",
            headers=owner_headers,
            files={"file": ("owner-file.txt", io.BytesIO(b"o" * owner_upload_size), "text/plain")},
        )
        assert r_upload.status_code == 200, r_upload.text

        thread_key = f"spaces/AAA/threads/{random_lower_string()}"
        event = build_message_event(
            thread_key=thread_key,
            text="what is my leave balance?",
            sender_email=sender["email"],
            attachments=[
                build_message_attachment(content_name="leave-request.txt", content_type="text/plain")
            ],
        )
        resp, _send_mock, fetch_mock = _post_chat(
            client, channel, signer, event, fetch_return_value=b"x" * sender_attachment_size
        )
    assert resp.status_code == 200
    fetch_mock.assert_awaited_once()

    hr_sessions = [s for s in list_sessions(client, owner_headers) if s["agent_id"] == hr_agent["id"]]
    assert len(hr_sessions) == 1, hr_sessions
    session = hr_sessions[0]
    assert session["user_id"] == owner["id"]

    message = _user_message(client, owner_headers, session["id"])
    files = message["files"]
    assert len(files) == 1, (
        "The sender's attachment was skipped — it must be charged against "
        "the SENDER's own storage usage, not the identity owner's (whose "
        f"quota was deliberately filled to {owner_upload_size}/{max_bytes} "
        "bytes by this test). files="
    ) + repr(files)
    file_id = files[0]["id"]

    assert _download(client, sender_headers, file_id).status_code == 200
    assert _download(client, owner_headers, file_id).status_code == 200
    _stranger, stranger_headers = create_random_user_with_headers(client)
    assert _download(client, stranger_headers, file_id).status_code == 403


# ---------------------------------------------------------------------------
# 12. Quota exhaustion, MIME rejection, count cap — skip, never a 5xx
# ---------------------------------------------------------------------------


def test_quota_exhaustion_mime_rejection_and_count_cap_each_skip_without_a_5xx(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    channel = _channel(client, superuser_token_headers)
    signer = GoogleChatJWTSigner()

    # ── Phase 1: quota exhaustion ────────────────────────────────────────
    user1, headers1, agent1 = _known_sender_with_agent(client, superuser_token_headers)
    thread1 = f"spaces/AAA/threads/{random_lower_string()}"
    event1 = build_message_event(
        thread_key=thread1,
        text="my quota is full",
        sender_email=user1["email"],
        attachments=[build_message_attachment()],
    )
    with patch.object(settings, "UPLOAD_MAX_USER_STORAGE_GB", 0):
        resp1, _s1, _f1 = _post_chat(client, channel, signer, event1, fetch_return_value=b"small file")
    assert resp1.status_code == 200
    session1 = [s for s in list_sessions(client, headers1) if s["agent_id"] == agent1["id"]][0]
    msg1 = _user_message(client, headers1, session1["id"])
    assert msg1["files"] == []
    assert "your storage is full" in (msg1["content"] or "")

    # ── Phase 2: MIME rejection ──────────────────────────────────────────
    user2, headers2, agent2 = _known_sender_with_agent(client, superuser_token_headers)
    thread2 = f"spaces/AAA/threads/{random_lower_string()}"
    event2 = build_message_event(
        thread_key=thread2,
        text="an unsupported file",
        sender_email=user2["email"],
        attachments=[
            build_message_attachment(content_name="app.exe", content_type="application/x-msdownload")
        ],
    )
    resp2, _s2, _f2 = _post_chat(client, channel, signer, event2, fetch_return_value=b"MZ...")
    assert resp2.status_code == 200
    session2 = [s for s in list_sessions(client, headers2) if s["agent_id"] == agent2["id"]][0]
    msg2 = _user_message(client, headers2, session2["id"])
    assert msg2["files"] == []
    assert "file type isn't supported" in (msg2["content"] or "")

    # ── Phase 3: count cap ───────────────────────────────────────────────
    user3, headers3, agent3 = _known_sender_with_agent(client, superuser_token_headers)
    thread3 = f"spaces/AAA/threads/{random_lower_string()}"
    event3 = build_message_event(
        thread_key=thread3,
        text="two files, only one allowed",
        sender_email=user3["email"],
        attachments=[
            build_message_attachment(content_name="first.pdf"),
            build_message_attachment(content_name="second.pdf"),
        ],
    )
    with patch.object(settings, "CHANNEL_ATTACHMENT_MAX_PER_MESSAGE", 1):
        resp3, _s3, fetch3 = _post_chat(
            client, channel, signer, event3, fetch_return_value=b"%PDF-1.4"
        )
    assert resp3.status_code == 200
    fetch3.assert_awaited_once()  # the excess ref is skipped WITHOUT being fetched
    session3 = [s for s in list_sessions(client, headers3) if s["agent_id"] == agent3["id"]][0]
    msg3 = _user_message(client, headers3, session3["id"])
    assert len(msg3["files"]) == 1
    assert msg3["files"][0]["filename"] == "first.pdf"
    text3 = msg3["content"] or ""
    assert "second.pdf" in text3
    assert "more than 1 attachments in one message" in text3
