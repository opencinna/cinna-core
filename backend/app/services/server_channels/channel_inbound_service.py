"""The inbound pipeline: webhook request → agent session.

This module is the security boundary of the feature. The webhook it serves is
platform-unauthenticated by design — anyone on the internet can POST to it —
so the ordering below is load-bearing, not stylistic:

    1. resolve channel      unknown/disabled token → 404, no detail, no oracle
    2. VERIFY (fail closed) adapter proves the platform sent this; nothing
                            above this line has parsed or trusted the payload
    3. dedup                webhook redelivery must not double-send
    4. whitelist            NULL/empty denies everyone; "*" is the only
                            blanket allow
    5. resolve user         auto-register only if the channel opted in
    6. channel policy       admin kill switch AND access grant AND the user's
                            own toggle; resolved once, here, and carried into
                            routing as plain data
    6.5 attachments         the sender's files become FileUpload rows owned by
                            THEM. Below every gate above on purpose: nothing
                            is fetched, stored, or charged to a quota before
                            the sender is admitted
    7. binding / routing    session work, off the request path

Steps 1–6 are cheap and run inline. Everything from 7 on can be slow (an LLM
routing call, an install, an environment build), so it runs as a background
task and the webhook answers immediately. The real reply arrives
asynchronously through ``ChannelOutboundService``.

What that immediate answer *is* depends on the transport. A **decline** is
always the sync response: it needs no outbound credential, which is what lets
a channel refuse a sender before setup is finished. An **accepted** message
acks with an empty body on a transport that can run a status notice, and the
background task posts the narration itself — see ``REPLY_WORKING`` and the
notice helpers on ``ChannelOutboundService``. The reason is not stylistic:
Google Chat creates the synchronous message but never tells us its id, so a
notice answered inline can be neither rewritten as the work advances nor
removed when the answer lands, which is the whole behaviour. Transports
without that capability keep the sync acknowledgement they always had.

The list splits at the chokepoint, and so does the code. ``handle_inbound``
owns steps 1–2 — the parts that are webhook-shaped, because only a webhook has
a token to resolve and a request to verify. ``process_inbound`` owns everything
from there and is transport-agnostic: a polled transport authenticates inside
its own ``poll`` (see ``PolledChannelTransport.poll``, which restates step 2's
promise for a pull driver) and enters the pipeline at the second method. What
does **not** change is the ordering below the split, or the rule that nothing
under it re-checks the sender.

**Step 6 gates every path below it, not only new threads.** A sender whose
access was revoked — the admin disabled the channel, withdrew their grant, or
they switched the channel off themselves — is declined on their *existing*
thread too. An already-bound conversation that keeps answering after access is
taken away is a security surprise, and ``ServerChannel.enabled`` is documented
as an absolute kill switch, which it would not be if the threads it had already
opened went on working. The decline uses the whitelist miss's reply verbatim;
see ``REPLY_DENIED``.

Trust model: the sender's email comes from a payload the adapter verified was
signed by the platform. That is the same trust tier the email integration
extends to IMAP, and it is what lets a whitelisted address be treated as an
identity. Sessions are owned by the sender's own platform user, so the blast
radius of a whitelist mistake is one empty auto-created account.

**One deliberate exception to that last sentence, since Phase 3 of
``docs/plans/channels_identity_unification/``: identity routing.** When a
sender has switched ``allow_identity_routing`` on and addresses a person who
shared an identity with them, routing may select *that person's* agent, and the
session is then owned by the identity owner rather than by the sender — it is
their agent, their space, their credentials, and their session list. It is
opt-in on both sides (the owner shares the identity and assigns it; the sender
turns identity routing on for the channel and keeps the per-person toggle), and
every message re-verifies the grant, so revocation bites immediately rather than
at the next thread. What does **not** change is the thread binding: it stays the
sender's, because "this thread belongs to this person" is what stops one member
of a group space from posting into another's conversation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.models import (
    Agent,
    AgentBundle,
    AgentEnvironment,
    CHANNEL_BINDING_ACTIVE,
    CHANNEL_BINDING_FAILED,
    CHANNEL_BINDING_PENDING_INSTALL,
    ChannelAccessPolicy,
    ChannelThreadBinding,
    IdentityGrant,
    SecurityEventCreate,
    ServerChannel,
    SessionSender,
    User,
)
from app.models.events import security_event as security_event_constants
from app.models.files.file_upload import FileUpload
from app.services.common.email_patterns import match_email_pattern
from app.services.server_channels.channel_attachment_service import (
    ChannelAttachmentResult,
    ChannelAttachmentService,
    SkippedAttachment,
)
from app.services.server_channels.channel_control_commands import (
    execute_control_command,
    match_control_command,
)
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_INSTALLING,
    DEBUG_NO_MATCH,
    DEBUG_RECEIVED,
    DEBUG_REJECTED,
    DEBUG_REPLIED,
    DEBUG_ROUTED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)
from app.services.routing import routing_trace
from app.services.sessions.channel_ingestion_service import (
    ChannelDecline,
    ChannelIngestionService,
    NoActiveEnvironmentError,
)
from app.services.server_channels.adapters.base import (
    ChannelAdapter,
    ChannelInboundMessage,
    ChannelVerificationError,
)
from app.services.server_channels.adapters.registry import get_adapter
from app.services.server_channels.channel_outbound_service import (
    ChannelOutboundService,
)
from app.services.server_channels.channel_policy_service import (
    ChannelPolicyService,
    ResolvedChannelPolicy,
)
from app.services.server_channels.channel_routing_service import (
    ChannelRoutingService,
)
from app.services.server_channels.server_channel_service import ServerChannelService
from app.services.users.user_service import UserService

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.models import Session as ChatSession

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelInbound]"


def _decision_detail(decision_id: uuid.UUID | None) -> dict[str, str]:
    """``{"trace_id": ...}`` — but only when a durable row was actually written.

    The live debug feed publishes this so an admin can jump from "no match" to
    the full routing trace. Emitting the id unconditionally would advertise a
    link that ``GET /admin/routing/traces/{id}`` 404s on whenever tracing is
    switched off, the origin is filtered out, or a persist was swallowed — a
    dead link in a diagnostic panel is worse than no link.
    """
    return {"trace_id": str(decision_id)} if decision_id else {}


def _debug_channel_key(channel: ServerChannel) -> str | None:
    """``channel``'s debug-buffer key, or ``None``. **Total by construction.**

    §11a Rule 2 in its narrowest useful form. ``channel.id`` looks like a field
    read and is not: every caller that reaches ``_reply`` does so after a
    ``db.commit()``, which expires the instance, so the read is a lazy reload
    and a reload of a concurrently deleted row raises ``ObjectDeletedError``.

    ``_route_and_bind`` solves the same problem by reading the id *before* the
    commit, while the instance is fresh. That is the better fix and it is not
    available here: the commit lives in the caller — six different callers,
    several frames up. So the read is made total instead, which is the same
    contract :func:`app.services.routing.routing_trace.clamp` and
    :func:`~app.services.routing.routing_trace.describe_exception` hold, for
    the same reason: it is evaluated as a bare argument expression, and
    ``ChannelDebugBuffer.record``'s own never-raises guard cannot reach back
    out to cover it.

    Returning ``None`` (rather than a placeholder key) is deliberate. An event
    filed under a key no channel has is unreachable through
    ``list_events(channel_id)``: it would look recorded while being invisible,
    and it would let a hostile or churning workload accrete buffers nobody can
    read. The caller drops the event instead — and this logs, so the drop is
    itself observable rather than silent.
    """
    try:
        return str(channel.id)
    except Exception:  # noqa: BLE001 — a debug aid must never break delivery
        logger.warning(
            "%s Could not identify a channel for the debug buffer (instance "
            "expired and its row is gone?) — dropping the event rather than "
            "filing it under a key no panel reads",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


def _status_notice_supported(channel: ServerChannel) -> bool:
    """Whether ``channel``'s transport can run a mutating status notice.

    Total, like every other reader in this file's instrumentation band: a
    channel whose adapter cannot be resolved, or whose row went away, answers
    False and the caller simply says less. False is always a safe answer here —
    it means "narrate the old way, or not at all" — while a raise would come
    out of a progress notice and abort work that had already succeeded.
    """
    try:
        return get_adapter(channel.channel_type).capabilities.supports_status_notice
    except Exception:  # noqa: BLE001 — see the docstring
        return False


def _outbound_credentials_configured(channel: ServerChannel) -> bool:
    """Whether ``channel`` has something to send an outbound message *with*.

    Delegates to ``ServerChannelService.has_outbound_credentials``, which is
    the one place that knows where a given transport keeps its credential
    (``encrypted_secrets`` for a webhook transport, the referenced SMTP server
    for email) — a fact this module must not learn a second time.

    Total, like every other reader in this band, and it degrades toward
    **saying something**: an unanswerable channel reports False, which sends
    the caller down the synchronous-ack path. That is a possible duplicate
    message in the worst case, against total silence in the other direction.
    """
    try:
        return ServerChannelService.has_outbound_credentials(channel)
    except Exception:  # noqa: BLE001 — see the docstring
        return False


#: Which routing-trace ``origin`` each transport's decisions carry, keyed on
#: ``ChannelAdapter.channel_type``.
#:
#: One mapping rather than a literal at the ``decide()`` call, so a fourth
#: transport cannot arrive without somebody deciding what its traces are
#: called — which is the question that went unasked when email started routing
#: and silently inherited ``decide``'s default.
#:
#: ``google_chat`` keeps ``server_channel`` deliberately. It is the value every
#: trace that path has ever written carries, and the one the admin origin
#: filter, the feature docs and the trace tests already agree on; renaming it
#: would move all of that for no gain. ``email`` gets its own as of phase 6 of
#: the channels & identity unification — see ``routing_trace.ORIGIN_EMAIL`` for
#: what that changes about rows written before it.
#:
#: ``app_mcp`` is absent on purpose. It is a ``ServerChannel`` and it has an
#: origin of its own, but it does not route through this service:
#: ``AppMCPRoutingService.route_message`` opens its own capture. An entry here
#: would describe a path that does not exist.
_TRACE_ORIGIN_BY_CHANNEL_TYPE: dict[str, str] = {
    "google_chat": routing_trace.ORIGIN_SERVER_CHANNEL,
    "email": routing_trace.ORIGIN_EMAIL,
}


def _trace_origin(channel_type: str) -> str:
    """The routing-trace ``origin`` for one transport.

    An unmapped transport falls back to ``server_channel`` rather than raising.
    An origin is a label on a diagnostic: a transport that reaches routing
    without an entry above should still leave a readable trace rather than
    failing a delivery over one. The fallback is what makes the map safe to be
    incomplete; the comment above it is what keeps leaving it incomplete a
    decision rather than an oversight.
    """
    return _TRACE_ORIGIN_BY_CHANNEL_TYPE.get(
        channel_type, routing_trace.ORIGIN_SERVER_CHANNEL
    )


def _log_detail(exc: BaseException) -> str:
    """``exc``'s full text for the application **log**. Total by construction.

    Deliberately not :func:`~app.services.routing.routing_trace.describe_exception`,
    and the split is about audience. The debug buffer is a superuser *read*
    surface, so what goes there is de-tainted down to type and status; the
    application log is an operator surface where the adapter's actual complaint
    ("permission denied for space X") is the whole diagnosis. Dropping it in
    both places would leave nobody able to answer why a notice failed.

    Nor is it :func:`~app.services.routing.routing_trace.clamp`, which reaches
    ``if not text`` before its own guard and so is not total against a raising
    ``__bool__`` — one of the shapes §11a Rule 2 names.

    Why pre-format at all, when ``logging`` interpolates lazily and swallows
    its own formatting errors: pytest's ``LogCaptureHandler`` overrides
    ``handleError`` to *re-raise*. Passing a raw ``exc`` therefore behaves one
    way in production and another under test — and the way it behaves under
    test is "destroys the exception this handler was recording". A guard whose
    correctness depends on which logging handler is installed is not a guard.
    Found by firing the poison object, not by reading this code.
    """
    try:
        return str(exc)[:2000]
    except Exception:  # noqa: BLE001 — the point of the helper
        try:
            return f"<unprintable {type(exc).__name__}>"
        except Exception:  # noqa: BLE001 — poisoned metaclass; still total
            return "<unprintable>"


# Environment statuses that mean "ready to take a message now".
_ENV_READY = {"running"}
# Terminal failures: the install will not become usable on its own. Both are
# genuinely terminal, unlike `critical_state` (see the note in `_flush_one`) —
# waiting out the age cap on them would just delay the inevitable.
_ENV_FAILED = {"error", "deprecated"}
# A binding that never reaches `running` is failed after this long rather than
# retrying forever. Bounds the flush loop without needing an attempt column:
# at a 45s tick this is ~40 attempts.
_PENDING_MAX_AGE_SECONDS = 30 * 60

# Static, deliberately uninformative replies. None of these tell an
# unauthenticated caller anything they didn't already know.
REPLY_DENIED = (
    "Sorry, you don't have access to this assistant. "
    "Please contact your administrator."
)
REPLY_WELCOME = (
    "Hi! Send me a message describing what you need and I'll find the right "
    "assistant for you."
)
# The status-notice texts. On a transport with
# ``supports_status_notice`` these are successive states of ONE message, not
# four messages — which is why they read as states ("finding…", "setting up…",
# "working…") rather than as announcements, and why they are short: the person
# reads each one exactly once, in place.
REPLY_WORKING = "🔎 Finding the right assistant for you…"
REPLY_STILL_SETTING_UP = (
    "Still setting up your assistant — I'll answer here as soon as it's ready."
)
REPLY_NO_MATCH = (
    "I couldn't find an assistant that can help with that. "
    "Please contact your administrator."
)
REPLY_INSTALLING = (
    "⚙️ Setting up **{agent_name}** for you — first-time setup takes a few "
    "minutes. I'll reply here when it's ready."
)
REPLY_READY = "💬 Your assistant is ready — working on your message…"
# The plain spinner: a thread that is already bound and simply working. Posted
# only where it can be taken away again (``supports_status_notice``); a
# transport that would leave it standing forever is better off silent.
REPLY_WORKING_ON_IT = "💬 Working on your message…"
REPLY_SETUP_FAILED = (
    "Sorry — setting up your assistant failed. Please contact your "
    "administrator."
)
REPLY_TOO_MANY_QUEUED = (
    "I've got a lot queued up for you already — I'll work through those first, "
    "then you can send this again."
)
REPLY_THREAD_OWNED = (
    "This conversation belongs to someone else. Please start a new thread and "
    "I'll set you up with your own assistant."
)
# The `/stop` decline — see ``channel_control_commands.handle_stop``. It is
# reached only after the ownership gate, so the person asking is the person who
# owns the thread, and what it reports is a fact about *their own*
# conversation: nothing is streaming in it. It reveals nothing about the
# server, no more than the silence a successful `/stop` answers with does, so
# it does not belong to the indistinguishable-declines family above.
#
# A successful `/stop` deliberately has no text of its own: the stopped marker
# that ``ChannelOutboundService.handle_stream_interrupted`` settles into the
# status notice is the acknowledgement.
REPLY_NOTHING_TO_STOP = "There's nothing running right now."
# The ONE reply in this module that is deliberately specific.
#
# Every other decline above is uninformative on purpose — "you are not
# whitelisted", "your grant was withdrawn" and "this channel is switched off"
# must be one answer, or the reply becomes an oracle an external sender can
# enumerate a server's channel configuration with. A rejected attachment is not
# that kind of decision (plan §4.5): "your 40MB video exceeds the 25MB limit"
# tells the sender about *their own message* and reveals nothing about who else
# may use this channel. And it is the one case where saying nothing looks
# exactly like the platform being broken — the sender's entire message was the
# file, so silence is indistinguishable from a dropped message.
#
# It does NOT go through ``REPLY_DENIED``, and that path is untouched.
#
# **Two routes, one per transport shape.** This text is handed to
# ``adapter.build_sync_response``, which is a real reply on a webhook transport
# and **inert** on a polled one — ``PolledChannelTransport`` does not override
# it and the base returns ``{}``. So it is also handed to
# ``adapter.send_rejection_notice``, which is the mirror image: a no-op on the
# base (and therefore on Google Chat, whose sync response already carried it)
# and a real outbound send on email. Exactly one of the two reaches any given
# sender.
#
# The email send is the *only* sender-facing text that leaves that transport
# outside an agent's answer, and the reason it is not a hole in the
# no-declines-on-a-pull-channel rule is the paragraph above: this branch is
# reached only after every security gate has already admitted the sender, and
# what it says describes their own message and nothing about the server.
REPLY_ATTACHMENTS_REJECTED = (
    "Sorry — I couldn't accept {details}. There was no text in your message "
    "either, so there's nothing for me to work on. Please send it again, or "
    "tell me in text what you need."
)
# The closing advice is deliberately NEUTRAL. It used to say "as a smaller or
# differently-formatted file", which is sound guidance for ``too_large`` and
# ``type_not_allowed`` and simply wrong for ``poll_budget_exhausted`` — a
# transient budget exhaustion that a plain resend fixes, where telling someone
# to shrink their file sends them to solve a problem they do not have. The
# reason-specific hint belongs to the reason: every entry in ``_reason_phrase``
# carries its own, rendered into ``{details}``.

# ======================================================================
# Step 6.5 — inbound attachments
# ======================================================================
#
# The pipeline's half of the attachment feature. ``ChannelAttachmentService``
# owns turning refs into ``FileUpload`` rows; everything here is about what the
# *humans* see afterwards — the sender, the person reading the transcript
# later, and the admin reading the debug feed.


#: How much of the skip list any *sender-visible* surface renders — the
#: transcript note and the decline reply. Filenames are sender-supplied text
#: on an unauthenticated ingress and the transport has its own message-length
#: limit; see ``_describe_skipped``.
_MAX_SKIP_TEXT_CHARS = 500


#: Sender-facing prose for a skip code the mapping below does not know.
#:
#: Deliberately vague, and that is the point — see ``_reason_phrase``.
_UNKNOWN_REASON_PHRASE = "the file could not be accepted"


def _reason_phrase(reason: str) -> str:
    """One skip code rendered as a sentence fragment a sender can act on.

    The codes are a contract (``services/files/attachment_limits.py``,
    ``channel_attachment_service``, and each adapter's own fetch failures);
    the prose is not, and lives here because this module is the only place
    that renders it to a person.

    **An unrecognised code falls back to a generic sentence, never to the code
    itself.** This surface is a *sender's* transcript on an unauthenticated
    ingress, and a raw token is an internal identifier leaking outward — it
    tells an external party nothing they can act on while naming a piece of
    the platform's vocabulary. The pressure to echo the token is real (a
    reader who sees ``drive_file`` can at least search for it), and it is
    answered on the other surface instead: ``_attachment_detail`` puts the
    **exact code** in the admin debug feed, which is superuser-only and is
    where an operator diagnoses this. Prose outward, tokens inward — the same
    split ``_reply`` and ``_log_detail`` already draw in this file.

    The safety property that buys: an adapter can invent a reason without a
    matching entry here, and the worst outcome is a vague sentence rather than
    a leaked identifier. That is not hypothetical — Phase 1 added
    ``storage_error`` and ``upstream_error``, which the plan never named, the
    adapters added ``invalid_handle`` and ``poll_budget_exhausted``, and the
    materialiser later split ``fetch_budget_exhausted`` out of ``timeout``.
    Every code any of them can currently produce is listed below; the fallback
    exists for the next one, not for these.
    """
    # Every value is a **fragment**, never a sentence: both surfaces that
    # render it already supply the frame ("… could not be accepted: NAME
    # (<fragment>)"). A value that repeats the frame reads as a stutter in the
    # sender's transcript, which is how the first draft of
    # ``poll_budget_exhausted`` was caught.
    phrases = {
        # ---- Refused by validation: the sender's to fix ----
        "too_large": (
            f"file exceeds the {settings.CHANNEL_ATTACHMENT_MAX_FILE_MB}MB limit"
        ),
        "type_not_allowed": "file type isn't supported",
        "aggregate_limit": (
            "message exceeds the "
            f"{settings.CHANNEL_ATTACHMENT_MAX_AGGREGATE_MB}MB total attachment "
            "limit"
        ),
        "quota_exceeded": "your storage is full",
        "too_many_attachments": (
            "more than "
            f"{settings.CHANNEL_ATTACHMENT_MAX_PER_MESSAGE} attachments in one "
            "message"
        ),
        # ---- Failed to fetch or store: mostly the operator's ----
        "no_content": "the file had no readable content",
        "timeout": "downloading the file timed out",
        "upstream_error": "the file couldn't be downloaded",
        "not_found": "the file was no longer available",
        "forbidden": "I wasn't allowed to download the file",
        "storage_error": "the file couldn't be saved",
        # Says nothing about the handle, the URL it would have been built
        # into, or which shape rule rejected it. §4.4: the media token never
        # reaches a log line, let alone a sender's transcript — and the
        # validation rule is a description of our own guard, which is not the
        # sender's business and is a hint to anyone probing it.
        "invalid_handle": "the attachment reference wasn't usable",
        # ---- Guidance, not just a refusal (§6.5) ----
        "drive_file": (
            "I can't open Google Drive attachments — please attach the file "
            "directly"
        ),
        # Nothing broke: the fetch was still queued on the limiter and never
        # issued a request, so there is no fault for an operator to find —
        # filing it under a fetch/store failure would send an admin hunting one
        # that does not exist. Distinct from ``timeout`` on purpose, and this
        # is why: nothing was ever downloaded. The message brought more files
        # than one whole-message fetch budget could work through, so the advice
        # is about the message, which is the only part the sender controls.
        # The "it needs a DIFFERENT message, so it must be Refused" reading was
        # considered and rejected. ``drive_file`` just above is the governing
        # precedent, not an anomaly: it too asks the sender to change what they
        # send, nothing broke, and it is guidance. Refused is for validation
        # that rejected the message, not for every case where the fix is the
        # sender's.
        # Not to be merged with ``poll_budget_exhausted`` below either, however
        # alike the two names read — same family, different remedy. Both are
        # "a budget ran out", but that one clears by re-sending the SAME
        # message on the next poll tick's fresh budget, while this one needs a
        # SMALLER message; re-sending it unchanged just exhausts the same
        # budget again.
        "fetch_budget_exhausted": (
            "there wasn't time to download it — please send fewer files at once"
        ),
        # The one genuinely TRANSIENT reason in the vocabulary, and the wording
        # earns its length because of it. Every other code above describes
        # something re-sending will not change; this one is a mail server that
        # had too much in flight on a single poll tick, and the very next tick
        # has a fresh budget. Telling this sender to "try again" is the only
        # place in this mapping where that advice is true, so it is said
        # plainly rather than folded into a generic failure.
        "poll_budget_exhausted": (
            "the mail server was handling too much at once — please send the "
            "file again"
        ),
    }
    return phrases.get(reason, _UNKNOWN_REASON_PHRASE)


def _describe_skipped(skipped: list[SkippedAttachment]) -> str:
    """``"report.mp4 (file exceeds the 25MB limit), logo.svg (…)"``.

    ``filename`` is already the sanitised name (``SkippedAttachment``'s own
    contract), which matters because this string goes into stored message
    content and into a sender-visible reply.

    **Capped**, for the same reason the debug feed's version is. Sanitisation
    bounds a filename's *characters*, not its length, and up to
    ``CHANNEL_ATTACHMENT_MAX_PER_MESSAGE`` of them are concatenated here — into
    a Chat message that has a length limit of its own. An overrun would turn
    the one reply this module exists to guarantee into a failed send, which is
    exactly the silence it is meant to prevent.
    """
    rendered = ", ".join(
        f"{item.filename} ({_reason_phrase(item.reason)})" for item in skipped
    )
    if len(rendered) > _MAX_SKIP_TEXT_CHARS:
        rendered = rendered[: _MAX_SKIP_TEXT_CHARS - 1] + "…"
    return rendered


def _compose_inbound_text(text: str, skipped: list[SkippedAttachment]) -> str:
    """``text`` plus a note naming what could not be accepted.

    Returns ``text`` unchanged when nothing was skipped — which is the common
    case and the one that must stay byte-for-byte identical to today.

    **The note lands in the STORED ``message.content``, and that is the
    decision, not an accident** (plan §5.4). The session owner — or, on an
    identity-routed thread, a person who did not send the message at all —
    reads this transcript later and needs to see that the sender's message was
    incomplete. There is no other durable surface where they would: the debug
    feed is an in-memory ring buffer that empties on restart, and hiding the
    note in agent-bound content only would make the omission invisible to every
    human who reads the conversation afterwards.

    An empty ``text`` yields the note alone, which is exactly the
    attachment-only case the caller handles specially.
    """
    if not skipped:
        return text
    noun = "attachment" if len(skipped) == 1 else "attachments"
    note = (
        f"⚠️ {len(skipped)} {noun} could not be accepted: "
        f"{_describe_skipped(skipped)}"
    )
    return f"{text}\n\n{note}" if text else note


def _attachments_rejected_reply(skipped: list[SkippedAttachment]) -> str:
    """The sender-facing decline for a message that was *only* attachments.

    ``skipped`` can legitimately be **empty** here: the caller's predicate is
    keyed on the message having carried attachments, not on the materialiser
    having produced reasons, so an adapter that hands over refs while declaring
    it carries none lands in this branch with nothing to name. The sender is
    still owed an answer — a vaguer one is far better than silence — so the
    empty case degrades to "your attachments" rather than rendering
    ``"0 attachments: "``.
    """
    if not skipped:
        return REPLY_ATTACHMENTS_REJECTED.format(details="your attachments")
    noun = "attachment" if len(skipped) == 1 else "attachments"
    return REPLY_ATTACHMENTS_REJECTED.format(
        details=f"{len(skipped)} {noun}: {_describe_skipped(skipped)}"
    )


#: How much of the skip list the debug feed renders. Filenames are
#: sender-supplied text on an unauthenticated ingress, and the panel shows
#: ``detail`` as one ``k=v`` line per key.
#:
#: Equal to ``_MAX_SKIP_TEXT_CHARS`` today and deliberately a separate knob:
#: this one answers to an admin panel's layout, that one to a chat transport's
#: message-length limit. They are free to diverge and neither should be
#: "simplified" into the other.
_MAX_SKIP_DETAIL_CHARS = 500


def _attachment_detail(
    *, accepted: int, skipped: list[SkippedAttachment]
) -> dict[str, str]:
    """The attachment keys for an inbound ``ChannelDebugBuffer`` record.

    **Every value is a string, deliberately.** ``ChannelDebugEvent.detail`` is
    typed ``dict[str, str]``, the admin panel renders it generically as ``k=v``
    pairs, and that shape is already in the generated OpenAPI client. Widening
    it to ``dict[str, Any]`` to carry the skip list as objects would ripple
    into ``schemas.gen.ts`` and cost this feature the "no client regeneration"
    property plan §6.2 states as a guarantee. So the counts are stringified and
    the skip list is flattened into one readable line instead.

    ``attachment_skips`` is omitted entirely when nothing was skipped, rather
    than rendered as an empty value: the panel would otherwise show a bare
    ``attachment_skips=`` on every message that carried a file successfully.
    """
    detail = {
        "attachments_accepted": str(accepted),
        "attachments_skipped": str(len(skipped)),
    }
    if skipped:
        rendered = "; ".join(f"{item.filename} ({item.reason})" for item in skipped)
        if len(rendered) > _MAX_SKIP_DETAIL_CHARS:
            rendered = rendered[: _MAX_SKIP_DETAIL_CHARS - 1] + "…"
        # Raw reason CODES here, not the prose of ``_reason_phrase``: the feed
        # groups refused-by-validation apart from failed-to-fetch by exact
        # token, and an admin's next action differs between the two families
        # (the first is the sender's to fix, the second is the operator's).
        detail["attachment_skips"] = rendered
    return detail


def _attachment_summary(*, accepted: int, skipped: int) -> str:
    """The debug feed's one-line headline for a message that carried files."""
    files = "file" if accepted == 1 else "files"
    if skipped:
        return f"{accepted} {files} accepted, {skipped} skipped"
    return f"{accepted} {files} accepted"


def _parse_parked_file_ids(raw: Any, binding_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Read ``file_ids`` back out of a parked entry. **Total.**

    The parked queue is a JSON column, so everything about the value is
    untrusted at read time — including its *absence*. A binding parked before
    this feature shipped drains after it and simply has no such key, which is
    why the caller uses ``entry.get("file_ids")`` and this function treats
    ``None`` as "no files" rather than as an error (§3.2).

    A malformed id is dropped, individually, with a warning — never by failing
    the drain and never by discarding the entry. The entry is a real message
    somebody sent and is still waiting on; delivering it without one attachment
    is a visible partial loss, while dropping it is a silent total one. The
    same trade the rest of this feature makes everywhere else.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "%s Parked entry for binding %s has a non-list file_ids value — "
            "delivering the message without its attachments",
            _LOG_PREFIX,
            binding_id,
        )
        return []
    parsed: list[uuid.UUID] = []
    for value in raw:
        try:
            parsed.append(uuid.UUID(str(value)))
        except Exception:  # noqa: BLE001 — one bad id must not lose a message
            logger.warning(
                "%s Dropping a malformed parked file id on binding %s",
                _LOG_PREFIX,
                binding_id,
            )
    return parsed


def _surviving_parked_file_ids(
    db: DBSession, file_ids: list[uuid.UUID], binding_id: uuid.UUID | None
) -> tuple[list[uuid.UUID], int]:
    """The parked ids whose ``file_uploads`` rows still exist, and how many went.

    **Why this exists at all.** A parked message's rows are ``temporary`` and
    ``cleanup_abandoned_temp_files`` reclaims them after 24h. Plan §9 says they
    "survive well inside the 24h GC window", which is true of the normal case
    and says nothing about the abnormal one two rows above it in the same
    table: a binding whose install never finished stays parked past the window,
    and its files are then gone while its *text* is still sitting there waiting
    to be delivered.

    Without this filter that text goes too. ``prepare_user_message_with_files``
    raises ``MessageServiceError("Some files not found")`` when any single id no
    longer resolves, ``_ingest_or_fail`` turns that into a failed binding and a
    generic setup-failed reply, and the re-route clears the parked entry — a
    **silent total loss where a partial one was available**. That is the exact
    trade this feature refuses everywhere else, ``_parse_parked_file_ids`` ten
    lines up included.

    **Total, and it degrades toward delivering.** A failed existence query
    leaves the ids untouched: that is today's behaviour, and guessing that
    every file is gone on the strength of a broken ``SELECT`` would throw away
    attachments that are sitting right there.
    """
    if not file_ids:
        return [], 0
    try:
        statement = select(FileUpload.id).where(
            FileUpload.id.in_(file_ids)  # type: ignore[attr-defined]
        )
        found = set(db.exec(statement).all())
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not check which parked files still exist on binding %s — "
            "delivering the message with its ids as recorded",
            _LOG_PREFIX,
            binding_id,
            exc_info=True,
        )
        return list(file_ids), 0

    surviving = [file_id for file_id in file_ids if file_id in found]
    missing = len(file_ids) - len(surviving)
    if missing:
        logger.warning(
            "%s %d parked attachment(s) on binding %s no longer exist (garbage "
            "collected while the binding waited); delivering the message with "
            "the %d that remain",
            _LOG_PREFIX,
            missing,
            binding_id,
            len(surviving),
        )
    return surviving, missing


def _compose_drained_text(text: str, missing: int) -> str:
    """``text`` plus a note that some of its attachments no longer exist.

    Same placement decision as :func:`_compose_inbound_text` and for the same
    reason: the note lands in the stored ``message.content``, because the
    transcript is the only durable surface where a later reader can learn that
    what arrived was incomplete. The filenames are not available — the rows
    that carried them are what went away — so the note counts rather than
    names.

    An attachment-only parked message whose files all vanished yields the note
    alone, which is what keeps it a *delivered* message rather than an entry
    that is silently dropped for having neither text nor files.
    """
    if missing <= 0:
        return text
    noun, verb = ("attachment", "is") if missing == 1 else ("attachments", "are")
    note = (
        f"⚠️ {missing} {noun} from this message expired before it could be "
        f"delivered and {verb} no longer available"
    )
    return f"{text}\n\n{note}" if text else note


def _attachment_classification_text(attachments: ChannelAttachmentResult) -> str:
    """What to route on when the sender sent files and no words.

    ``"(sent 3 files: a.pdf, b.png, c.csv)"``. Routing an empty string is a
    coin flip, and the filenames are the only signal the sender gave — so this
    is the classifier's input and **nothing else**. The stored message content
    is never rewritten to contain it (plan §5.4): the transcript records what
    the person actually sent.

    The names come off the result the materialiser already built. An earlier
    version re-read them from ``file_uploads`` because the result did not carry
    them; that was a ``SELECT`` on the synchronous webhook path to recover a
    value that had just been in hand, and — because a failed statement poisons
    a session the poll driver reuses across a whole tick — it needed a
    rollback guard of its own. Carrying the names on the result deletes the
    query, the guard and the failure mode together.

    Degrades to ``"(sent 3 files)"`` when the names are missing or do not line
    up with the ids. That case should not arise, and the fallback is kept
    anyway: the count is derived from ``file_ids``, which is the authoritative
    list, so a names/ids disagreement must not be able to misreport how many
    files a person sent.
    """
    count = len(attachments.file_ids)
    noun = "file" if count == 1 else "files"
    names = attachments.accepted_filenames
    if not names or len(names) != count:
        return f"(sent {count} {noun})"
    return f"(sent {count} {noun}: {', '.join(names)})"


# Cap on parked messages per binding: a thread that keeps talking while an
# environment builds should not grow an unbounded JSON blob.
_MAX_PARKED_MESSAGES = 20

# Throttle window for repeated denial / verification-failure security events
# from the same source, so a noisy prober can't flood the audit log.
_SECURITY_EVENT_THROTTLE_SECONDS = 300.0
_SECURITY_EVENT_MAX_KEYS = 5_000
_recent_security_events: dict[str, float] = {}

# In-process dedup for messages that arrive BEFORE a binding exists. The
# binding's `last_external_message_id` cannot cover first contact, and a
# redelivery there is expensive: two routing calls, possibly two installs.
# Bounded and TTL-swept — this key is derived from attacker-supplied input.
_RECENT_MESSAGE_TTL_SECONDS = 600.0
_RECENT_MESSAGE_MAX_KEYS = 5_000
_recent_message_ids: dict[str, float] = {}


class ChannelIngestProducedNoMessage(RuntimeError):
    """An ingest returned without creating a message, and did not raise.

    ``ChannelIngestionService.ingest_inbound_message`` maps
    ``SessionService.send_session_message``'s dict return onto an
    ``IngestionResult``, and a whole family of failures come back through it as
    a **soft** ``action="error"`` rather than as an exception: session vanished,
    agent has no active environment, environment activation failed, the file
    preparation step refused the message. The web-UI chat route wants that —
    it renders the friendly text — but on a channel it is a trap.

    **The trap.** ``_continue_thread`` decides whether to stamp
    ``binding.last_external_message_id`` on "did an exception escape
    ``_ingest_or_fail``", and ``_drain_parked`` decides whether to pop a parked
    entry the same way. Neither asks whether a message was actually created. So
    a soft error stamped the binding — or dropped the parked entry — exactly as
    a delivered message would, and the sender's message was gone: no message,
    no error reply, no debug-feed record, and no second chance either, because
    the next identical redelivery is now deduped as already-processed.

    This exception is what converts that silent, permanent loss into an
    ordinary visible failure: the binding is failed (so it self-heals and
    re-routes on the next message), the sender gets the standard setup-failed
    notice, and a parked queue stays parked for a later attempt instead of
    being consumed.

    **Deliberately not a ``ChannelDecline``.** The drain classifies on that
    type to mean "this will fail identically forever", and its disposition is
    to *drop* the whole parked queue. Nothing here is known to be permanent —
    a missing environment comes back, an activation retry succeeds — so it
    takes the transient arm, which keeps the messages.
    """


class ChannelNotFound(Exception):
    """Unknown or disabled webhook token. Route maps to 404."""


class ChannelInboundService:
    """Webhook → routing → session. Stateless; all state is in the binding."""

    # ==================================================================
    # Entry point
    # ==================================================================

    @staticmethod
    async def handle_inbound(
        *,
        db: DBSession,
        webhook_token: str,
        request: Request,
        body: bytes,
    ) -> dict[str, Any]:
        """Run the inbound pipeline. Returns the webhook's sync response body.

        The webhook half: steps 1–2 (resolve the channel by token, then
        verify), after which it hands off to ``process_inbound`` for the rest.
        Only the two steps here are webhook-shaped; keeping them separate is
        what lets a polled transport reuse everything below without a
        ``Request`` to hand it.

        Raises ``ChannelNotFound`` (404) and ``ChannelVerificationError`` (403);
        every other outcome is a 200 with a static body, because a channel that
        gets a non-2xx retries the event forever.
        """
        # ---- 1. Channel resolution (enabled-only; 404 covers both cases) ----
        channel = ServerChannelService.get_by_webhook_token(db, webhook_token)
        if channel is None:
            raise ChannelNotFound()

        adapter = get_adapter(channel.channel_type)

        # ---- 2. VERIFICATION — the first thing that touches the payload ----
        # Fails closed. Nothing below this line runs unless the platform's
        # signature over this exact body checked out.
        try:
            inbound = await adapter.verify_inbound(request, channel, body)
        except ChannelVerificationError as exc:
            # The only place the *reason* survives. The caller gets a
            # detail-free 403 (a specific reason is a probing oracle) and the
            # debug-feed summary below is hardcoded, so without this line every
            # subclass — a forged signature and a
            # ``ChannelTransportMisuseError`` alike — reads to an operator as
            # "signature verification failed", and the misuse case actively
            # mislabels itself. The log is the operator surface where the
            # distinction is the whole diagnosis.
            #
            # ``_debug_channel_key`` and ``_log_detail`` rather than ``channel.id``
            # and ``exc``: both are total by construction, for the reasons their
            # own docstrings give. A diagnostic that can raise while handling a
            # rejection would replace the rejection with a 500 — see
            # ``_deliver`` in ``channel_outbound_service`` for the same rule.
            logger.warning(
                "%s Inbound verification rejected for channel %s (403): %s",
                _LOG_PREFIX,
                _debug_channel_key(channel),
                _log_detail(exc),
            )
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"verify:{channel.id}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_VERIFICATION_FAILED,
                severity="high",
                details={"server_channel_id": str(channel.id)},
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Signature verification failed — request rejected (403)",
                detail={"stage": "verify"},
            )
            raise

        return await ChannelInboundService.process_inbound(
            db=db, channel=channel, adapter=adapter, inbound=inbound
        )

    @staticmethod
    async def process_inbound(
        *,
        db: DBSession,
        channel: ServerChannel,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
    ) -> dict[str, Any]:
        """The pipeline from step 3 down — everything after authentication.

        Split out of ``handle_inbound`` because the *chokepoint* moved, not the
        pipeline. A webhook transport authenticates in ``verify_inbound`` and
        arrives here; a polled transport authenticates inside ``poll``, against
        a source whose strength it documents, and arrives here too. Nothing
        from here down knows or cares which door the message came through, and
        the module docstring's ordering is load-bearing for both.

        **The caller is the authentication chokepoint.** Nothing below
        re-verifies the sender — ``inbound.sender_email`` is treated as the
        sender's identity from the first line, and it is what the whitelist,
        user resolution, auto-registration and identity routing all key on. A
        caller that reaches this method with an ``inbound`` it did not
        authenticate has voided the promise the whole module rests on.

        Returns the body the caller answers with. For a webhook that is the
        sync HTTP response. For a polled transport it is **inert**:
        ``PolledChannelTransport`` does not override ``build_sync_response``
        and the base default returns ``{}``, so every denial below —
        ``REPLY_DENIED``, ``REPLY_THREAD_OWNED`` and the rest — collapses to
        the same empty body as the branches that ack in silence.

        Every sync response that *is* rendered carries the thread the message
        arrived on. Google Chat posts an unthreaded one as a new top-level
        message in the space, so a denial that omitted it answered somewhere
        other than the conversation it was declining.

        That is the decided behaviour, not a gap left open. A polled transport
        has no sync-reply surface, and mailing declines back down the pull
        channel would be both a probing oracle (an unlisted sender learns which
        addresses the platform knows) and a spam amplifier (every unsolicited
        message earns a reply to an address the sender chose).

        Silent to the *sender*; never to the *operator*. Every denial branch
        writes a ``ChannelDebugBuffer`` record with its own summary and stage
        before returning, and that feed — not the response body — is where an
        admin diagnoses a channel that is dropping messages.
        """
        # ---- Non-message events: ack cheaply, never error ----
        if inbound.event_kind == "ignored":
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Authentic event the pipeline does not act on — acked",
                thread_key=inbound.thread_key,
                detail={"stage": "event_kind", "event_kind": "ignored"},
            )
            return {}
        if inbound.event_kind == "added_to_space":
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_RECEIVED,
                summary="App added to a space — welcome reply sent",
                thread_key=inbound.thread_key,
                detail={"stage": "added_to_space"},
            )
            # No ``thread_key``: an ``added_to_space`` event has no thread
            # yet, and a space-level welcome is the correct shape for it.
            return adapter.build_sync_response(REPLY_WELCOME)

        ChannelDebugBuffer.record(
            channel_id=channel.id,
            direction="inbound",
            kind=DEBUG_RECEIVED,
            summary="Message received and signature verified",
            sender_email=inbound.sender_email,
            sender_display_name=inbound.sender_display_name,
            thread_key=inbound.thread_key,
            text=inbound.text,
            detail={
                "external_message_id": inbound.external_message_id or "",
                "external_user_id": inbound.external_user_id or "",
            },
        )

        if not inbound.sender_email:
            # Authentic event, but the platform gave us no address to identify
            # the sender by (a consumer-Gmail user on Google Chat). There is
            # nothing to whitelist against, so it is a denial — answered 200
            # with the standard text, never a 403, which the channel would
            # retry forever.
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Sender has no verified email address — denied",
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={"stage": "sender_identity"},
            )
            return adapter.build_sync_response(REPLY_DENIED, inbound.thread_key)
        if not inbound.thread_key:
            # No binding key means nothing can be bound; ack and drop.
            return {}

        # ---- 3. Redelivery dedup ----
        binding = ChannelInboundService._get_binding(
            db, channel.id, inbound.thread_key
        )
        if (
            binding is not None
            and inbound.external_message_id
            and binding.last_external_message_id == inbound.external_message_id
        ):
            logger.info(
                "%s Duplicate delivery of message %s on channel %s — acking",
                _LOG_PREFIX,
                inbound.external_message_id,
                channel.id,
            )
            return {}

        # First contact has no binding to dedup against, and a redelivery there
        # is the expensive one: two routing calls and possibly two installs.
        #
        # Deliberately gated on `binding is None`. Once a binding exists the
        # durable stamp above is the only dedup, and it is stamped ONLY after a
        # successful ingest — so a redelivery of a message we failed to process
        # is a recovery opportunity. Running this in-process check there would
        # ack and drop that redelivery (it records the key at webhook time,
        # before processing), silently defeating the recovery path
        # `_continue_thread` and `_park_message` are written to preserve.
        if (
            binding is None
            and inbound.external_message_id
            and ChannelInboundService._seen_recently(
                f"{channel.id}:{inbound.external_message_id}"
            )
        ):
            logger.info(
                "%s Duplicate pre-binding delivery of %s on channel %s — acking",
                _LOG_PREFIX,
                inbound.external_message_id,
                channel.id,
            )
            return {}

        # ---- 4. Whitelist — fails closed ----
        # `match_email_pattern` returns False for a NULL/empty pattern string,
        # so an unconfigured channel denies everyone. "*" is the only way to
        # allow all verified senders.
        if not match_email_pattern(inbound.sender_email, channel.email_whitelist):
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"denied:{channel.id}:{inbound.sender_email}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_SENDER_DENIED,
                severity="medium",
                details={
                    "server_channel_id": str(channel.id),
                    "sender_email": inbound.sender_email,
                    "reason": "not_whitelisted",
                },
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "Sender is not allowed by the email whitelist — denied. "
                    "An empty whitelist denies everyone."
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={
                    "stage": "whitelist",
                    "whitelist": channel.email_whitelist or "(empty)",
                },
            )
            return adapter.build_sync_response(REPLY_DENIED, inbound.thread_key)

        # ---- 5. User resolution (auto-register only if opted in) ----
        user = await ChannelInboundService._resolve_user(
            db=db, channel=channel, inbound=inbound
        )
        if user is None:
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "No platform account for this sender and auto-registration "
                    "is off — denied"
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={"stage": "user_resolution"},
            )
            return adapter.build_sync_response(REPLY_DENIED, inbound.thread_key)

        # ---- 6. Channel policy — resolved once, for every path below ----
        #
        # ``describe`` rather than ``resolve``: it is the same work (``resolve``
        # delegates to it) and it hands back *which* of the three terms failed,
        # which the debug entry below is allowed to say and the sender's reply
        # is not. Routing takes ``.policy`` and nothing else, per that view's
        # own docstring.
        #
        # Necessarily **after** user resolution, which means a restricted
        # channel can auto-register an account and then decline it: a grant is
        # keyed by user id, so there is no user to ask about until one exists.
        # The blast radius is unchanged — one empty passwordless account for a
        # sender who was already through the whitelist.
        policy_view = ChannelPolicyService.describe(db, channel, user.id)
        policy = policy_view.policy
        if not policy.is_available:
            # Audited on the same throttled bucket as the whitelist miss, and
            # under the same event type: from the outside these are one event —
            # "a verified sender was turned away" — and splitting them would
            # mean an admin hunting a denial had two feeds to search. The
            # ``reason`` field is what tells them apart.
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"unavailable:{channel.id}:{user.id}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_SENDER_DENIED,
                severity="medium",
                details={
                    "server_channel_id": str(channel.id),
                    "sender_email": inbound.sender_email,
                    "reason": "channel_unavailable",
                },
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "This sender may not use this channel right now — denied. "
                    "The detail says which of the three terms failed: the "
                    "channel's own switch, their access to it, or their "
                    "per-user toggle."
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                # Every value here is a plain bool off a frozen dataclass, so
                # nothing in this argument list can reach the database (§11a
                # Rule 2). The feed is superuser-only, which is why it may be
                # specific where the reply must not be.
                detail={
                    "stage": "channel_policy",
                    "channel_enabled": str(policy_view.channel_enabled),
                    "is_granted": str(policy_view.is_granted),
                    "is_enabled_for_user": str(policy_view.is_enabled_for_user),
                    "is_enabled_inherited": str(policy_view.is_enabled_inherited),
                },
            )
            # The whitelist miss's reply, verbatim and deliberately. "You are
            # not granted this channel", "you are not whitelisted" and "this
            # channel is switched off" must be one answer to an unauthenticated
            # sender, or the reply becomes an oracle that enumerates a server's
            # channel configuration one probe at a time.
            return adapter.build_sync_response(REPLY_DENIED, inbound.thread_key)

        # ---- 6.5. Attachments — refs become FileUpload rows ----
        #
        # **This position is the security story of the feature** (plan §4.1).
        # It is below the rate limit and the body cap, below verification
        # (an attachment handle is attacker-influenced data, and fetching one
        # out of an unsigned payload would be a request the platform makes on
        # an attacker's behalf), below the redelivery dedup (materialising
        # first would duplicate every file on every Chat retry, against the
        # sender's quota), below the fail-closed whitelist (storage is a
        # resource; an unlisted sender gets none of it), below user resolution
        # (there is no ``FileUpload.user_id`` to write until a user exists) and
        # below the policy gate (a revoked sender writes nothing to disk).
        #
        # Nothing beneath it re-authenticates, and it never widens what an
        # earlier step decided. Moving it up is not a refactor.
        #
        # **What the dedup above does and does not cover.** On first contact
        # (``binding is None``) ``_seen_recently`` records the id at webhook
        # time, so a Chat retry never reaches this line twice. On an ALREADY
        # BOUND thread the only dedup is ``last_external_message_id``, and that
        # is stamped after a successful ingest, inside the background task
        # (see ``_continue_thread``) — deliberately, so a redelivery of a
        # message we failed to process stays a recovery opportunity. A retry
        # therefore does reach this line twice, and the duplicate *storage*
        # that would follow is closed inside ``materialize`` instead of by
        # narrowing the dedup: it keys on
        # ``(binding, external_message_id, position)`` and hands back the rows
        # the earlier delivery already wrote, so the sender's quota is charged
        # once however many times Google retries. Moving the stamp above this
        # call would have been the other way to close it, and it is the wrong
        # one — it marks a half-ingested message as seen and strands it. The
        # text-level double delivery is pre-existing and unchanged.
        #
        # ``owner_id`` is the **sender**, never the session owner — on an
        # identity-routed thread those differ, and owning the file as the
        # sender is what keeps provenance honest and charges the right person's
        # quota (§3.4). ``_ingest`` tells the attach path who that was, via
        # ``uploader_user_id``.
        # ---- Everything below step 6.5 needs reading BEFORE it ----
        #
        # §11a Rule 2, and this is the call site that makes it bite rather than
        # a precaution: ``materialize`` **commits** when it accepted anything,
        # and rolls back when its backstop fired. Both expire every instance in
        # this session, so ``channel.id`` / ``binding.status`` read afterwards
        # are lazy reloads — an extra ``SELECT`` on the common path, and an
        # ``ObjectDeletedError`` if the row went away in between. For the debug
        # records that is a raise inside an argument list that
        # ``ChannelDebugBuffer.record``'s own never-raises guard cannot reach;
        # for the step-7 dispatch it is a 5xx on the webhook, which Google
        # retries — straight into the duplicate-materialisation window the
        # comment above just described.
        #
        # Read here, while both instances are fresh, and used as plain values
        # from this line on. ``binding`` itself is still handed to the writers
        # below, which need the live instance; what must not happen is a
        # *decision* being made on a reloaded attribute.
        debug_channel_id = _debug_channel_key(channel)
        binding_user_id = binding.user_id if binding is not None else None
        binding_status = binding.status if binding is not None else None
        binding_id = binding.id if binding is not None else None
        # ``user`` and ``channel`` are expired by the same commit, and every
        # read of them below step 6.5 is on the synchronous webhook path. The
        # binding hoist alone left those two behind — hoisted here for exactly
        # the reasons above it, not for tidiness.
        user_id = user.id
        channel_id = channel.id
        # The two capability reads at the end of step 8-10 go through
        # ``channel`` as well. Both helpers are already total, so an expired
        # instance whose row vanished degrades to False rather than raising —
        # but False there means "ack in silence became ack with text", a
        # behaviour change decided by a race, and it still costs a reload
        # SELECT on the request path. Resolved here, while the instance is
        # fresh, and read as plain booleans from this line on. Both are
        # in-memory registry lookups plus attribute reads, so computing them
        # ahead of the branch that uses them costs nothing measurable.
        notice_supported = _status_notice_supported(channel)
        outbound_configured = _outbound_credentials_configured(channel)

        attachments = await ChannelAttachmentService.materialize(
            db=db, channel=channel, adapter=adapter, inbound=inbound, owner_id=user_id
        )
        text = _compose_inbound_text(inbound.text, attachments.skipped)
        file_ids = attachments.file_ids
        # The rows ``materialize`` reused from an earlier delivery of this same
        # external message rather than creating. They are already ``attached``,
        # and the session pipeline refuses an attached file unless it is named
        # — so this set travels with ``file_ids`` all the way to
        # ``prepare_user_message_with_files``. Empty on every first delivery,
        # which is every message but a retry.
        redelivered_file_ids = set(attachments.reused_file_ids)

        # The attachment-only total rejection: the sender wrote nothing, sent
        # files, and not one of them became a file. Named once and used twice,
        # so the debug branch and the decline branch cannot drift apart.
        #
        # Keyed on ``inbound.attachments`` and NOT on ``attachments.skipped``,
        # which would leave a hole: ``materialize`` returns an empty result
        # with an empty skip list when an adapter hands it refs while
        # declaring ``supports_inbound_attachments=False``. Under the narrower
        # predicate that message would ingest as an empty string — or route a
        # brand-new thread on one — and the sender would be told nothing at
        # all. Low probability (it needs an adapter bug, already logged one
        # layer down) and total is one term away, which is the whole argument
        # for spending the term.
        #
        # ``inbound.has_sender_text`` and NOT ``inbound.text.strip()``, and
        # that is the whole of the fix that made this branch reachable at all.
        # ``text`` is the string that will be *stored*; on a transport that
        # synthesises it, that is not the same question as "did the sender
        # write anything". Email's ``text`` is
        # ``format_email_as_message(...)``, which emits its ``--- Forwarded
        # email content ---`` markers and a ``From:`` line for every mail —
        # including one whose subject and body are both empty. So the old
        # predicate was FALSE for every email ever polled, and this branch,
        # written for the one transport that actually needs an out-of-band
        # reply, could never fire on it: an attachment-only mail whose every
        # attachment was refused ingested "successfully" as the wrapper text
        # plus a ⚠️ note, created a session, woke an agent, and told the
        # sender nothing. The adapter now declares the emptiness
        # (``sender_text_empty``) instead of the pipeline trying to recover it
        # from a formatted string.
        #
        # The predicate this reads as, and the one that is actually meant:
        # *this message produced no usable content for the agent*. Not "the
        # ingest raised" — an attachment-only rejection ingests perfectly
        # happily, which is precisely how it stayed invisible.
        all_attachments_rejected = (
            not inbound.has_sender_text
            and not file_ids
            and bool(inbound.attachments)
        )

        if (
            inbound.attachments
            and not all_attachments_rejected
            and debug_channel_id is not None
        ):
            # What arrived, for the operator. Kept separate from the
            # "message received" record above rather than folded into it: that
            # one is written before the whitelist and policy gates precisely so
            # a *denied* message still shows up in the feed, and moving it down
            # here to carry these counts would erase every denial from it.
            #
            # The total-rejection case below writes its own ``DEBUG_REJECTED``
            # with the same detail keys, so it is excluded here rather than
            # narrated twice.
            ChannelDebugBuffer.record(
                channel_id=debug_channel_id,
                direction="inbound",
                kind=DEBUG_RECEIVED,
                summary=_attachment_summary(
                    accepted=len(file_ids), skipped=len(attachments.skipped)
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={
                    "stage": "attachments",
                    **_attachment_detail(
                        accepted=len(file_ids), skipped=attachments.skipped
                    ),
                },
            )

        if all_attachments_rejected:
            # **Attachment-only, and nothing survived.** The sender's entire
            # message was files and none of them became one, so there is
            # literally nothing to route on and nothing to hand an agent.
            #
            # Answered specifically rather than through ``REPLY_DENIED``
            # (§4.5). This is not a security decline and is not subject to the
            # deliberately-indistinguishable rule: naming "your 40MB video
            # exceeds the 25MB limit" tells the sender about their own message
            # and reveals nothing about who else may use this channel. Doing
            # nothing here is the one case that looks exactly like the platform
            # being broken.
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="inbound",
                    kind=DEBUG_REJECTED,
                    summary=(
                        "Every attachment was refused and the message had no "
                        "text — declined with the reasons named"
                    ),
                    sender_email=inbound.sender_email,
                    sender_display_name=inbound.sender_display_name,
                    thread_key=inbound.thread_key,
                    detail={
                        "stage": "attachments_rejected",
                        **_attachment_detail(accepted=0, skipped=attachments.skipped),
                    },
                )
            #
            # Delivered **twice, by two disjoint routes**: the sync response
            # below (which a webhook transport renders in-thread and a polled
            # one silently discards) and the adapter's own outbound notice
            # (which only a polled transport implements). Exactly one of them
            # reaches any given sender — a transport that answered on both
            # would double the message, which is why the base
            # ``send_rejection_notice`` is a no-op and Google Chat does not
            # override it.
            rejection_reply = _attachments_rejected_reply(attachments.skipped)
            try:
                await adapter.send_rejection_notice(
                    db,
                    channel,
                    inbound,
                    # The resolved platform account, never the ``From:``
                    # header — on email that header is spoofable, and mailing
                    # it would let a forged sender aim the platform's mail at
                    # somebody else.
                    recipient_user_id=user_id,
                    text=rejection_reply,
                )
            except Exception:  # noqa: BLE001 — a notice never costs the message
                # The adapter contract says this cannot happen; the guard is
                # here because of *where* it runs. On a polled transport this
                # is inside a poll tick whose earlier messages are already
                # marked read on the mail server, and an escape would abandon
                # the tick and lose them for good.
                logger.warning(
                    "%s Could not send the attachment rejection notice on "
                    "channel %s — the sender is not told, but the message is "
                    "still declined and recorded",
                    _LOG_PREFIX,
                    channel_id,
                    exc_info=True,
                )
            return adapter.build_sync_response(rejection_reply, inbound.thread_key)

        # ---- 7. Binding dispatch ----
        #
        # Gated on the hoisted ``binding_id`` as well as on ``binding`` itself.
        # The two are set together, one line apart, but they are separate names
        # and the type checker cannot see the link — and spelling it out is not
        # only a formality: if it somehow did not hold, falling through to a
        # fresh routing pass is the safe direction, where an unchecked
        # ``binding_id`` would put ``None`` into a background task.
        if binding is not None and binding_id is not None:
            # A thread belongs to exactly one person. In a group space another
            # whitelisted member can post into a thread already bound to
            # someone else's session — injecting their text into a stranger's
            # agent session, whose reply would then be posted where they can
            # read it. Multi-user rooms need a participant model and an
            # owner-approval flow (a listed future enhancement); until then the
            # correct behaviour is to decline, not to silently route.
            #
            # ``binding_user_id`` / ``binding_status`` / ``binding_id`` rather
            # than the instance attributes: step 6.5 committed, so every read
            # off ``binding`` here would be a reload. See the hoist above.
            # ``inbound.thread_key`` replaces ``binding.thread_key`` for the
            # same reason and is the same value by construction — the binding
            # was looked up by it.
            if binding_user_id != user_id:
                logger.warning(
                    "%s User %s posted into thread %s bound to user %s — declining",
                    _LOG_PREFIX,
                    user_id,
                    inbound.thread_key,
                    binding_user_id,
                )
                return adapter.build_sync_response(
                    REPLY_THREAD_OWNED, inbound.thread_key
                )

            # ---- 7a. Channel control commands ----
            #
            # A tiny set of strings is addressed to the pipeline rather than to
            # the agent; `/stop` is the chat-thread equivalent of the web
            # client's stop button, which a chat thread has no way to render.
            # See ``channel_control_commands``.
            #
            # **Placement is the security argument.** Below the ownership
            # decline above, so the person stopping a stream is the person the
            # thread belongs to; below step 6, so a sender whose access was
            # revoked cannot reach it; below the whitelist, the verification
            # and the rate limit, like everything else at step 7.
            # ``interrupt_stream``'s documented contract is "the caller must
            # authorize session access first", and this is that authorization:
            # ``binding_user_id == user_id`` was just established. That holds
            # on an identity-routed thread too, where the session lives in the
            # identity owner's workspace — the *conversation* is still the
            # sender's, which is exactly what ``binding.user_id`` records, and
            # stopping one's own conversation is not a reach into somebody
            # else's space.
            #
            # Runs for a PENDING_INSTALL binding as well as an ACTIVE one. It
            # has nothing to stop there, and the handler says so; what matters
            # is that it does **not** fall through to ``_park_message``, where
            # a `/stop` would be queued and then replayed at the agent as a
            # message the moment the install finished. A `failed` binding is
            # answered the same way rather than falling through to the
            # re-routing self-heal below, which would send the literal text
            # "/stop" to a classifier and then to an agent.
            #
            # ``inbound.text``, not the composed ``text``: the latter can carry
            # the platform's own ⚠️ note about refused attachments, which would
            # stop an otherwise-bare `/stop` from matching.
            #
            # **Three terms beyond the match itself, each closing a way for
            # this branch to swallow something.** A command takes no arguments
            # and no files, so *any* attachment on the message makes it an
            # ordinary one — ``file_ids`` for the accepted ones (somebody who
            # sent files meant them to be read) and ``attachments.skipped`` for
            # the refused ones, because intercepting there would drop the ⚠️
            # note that is the only place the sender learns their file was
            # rejected. ``outbound_configured`` because BOTH of this command's
            # answers need the outbound credential — a successful `/stop`
            # speaks through the status notice, a failed one through
            # ``_reply`` — so on a channel that cannot send, intercepting turns
            # the message into silence with no observable difference from it
            # never arriving. That is the same bargain the new-thread ack below
            # strikes at ``if notice_supported and outbound_configured``;
            # falling through instead leaves today's behaviour, which is the
            # documented safe direction for anything this branch declines.
            command = match_control_command(inbound.text)
            if (
                command is not None
                and not file_ids
                and not attachments.skipped
                and outbound_configured
            ):
                # Redelivery. The bound-thread paths below tolerate a retry
                # because ``_continue_thread`` stamps
                # ``last_external_message_id`` only after a successful ingest
                # (see the note at step 6.5); this branch stamps nothing and
                # commits nothing, so without its own dedup a Chat retry runs
                # the command twice — and the second `/stop` finds the stream
                # it just stopped already gone and posts "there's nothing
                # running right now" directly under the stopped marker, which
                # is the doubled acknowledgement the silent-success design
                # exists to avoid. Own key namespace, so it cannot collide with
                # the pre-binding dedup at step 3.
                if inbound.external_message_id and ChannelInboundService._seen_recently(
                    f"{channel_id}:control:{inbound.external_message_id}"
                ):
                    logger.info(
                        "%s Duplicate delivery of control command on channel "
                        "%s — acking without re-running it",
                        _LOG_PREFIX,
                        channel_id,
                    )
                    return {}

                if debug_channel_id is not None:
                    ChannelDebugBuffer.record(
                        channel_id=debug_channel_id,
                        direction="inbound",
                        kind=DEBUG_RECEIVED,
                        # Every value here is a plain local — no ORM attribute
                        # is read in this argument list (§11a Rule 2).
                        summary=(
                            f"Control command '{command}' — handled by the "
                            f"pipeline, not sent to the assistant"
                        ),
                        sender_email=inbound.sender_email,
                        sender_display_name=inbound.sender_display_name,
                        thread_key=inbound.thread_key,
                        text=inbound.text,
                        detail={"stage": "control_command", "command": command},
                    )
                ChannelInboundService._schedule(
                    execute_control_command(command, binding_id=binding_id),
                    "channel_control_command",
                )
                # Silent ack, for the same reason the active-thread branch
                # below acks in silence: whatever the command has to say, it
                # says from its own task — a successful `/stop` through the
                # status notice, a failed one through ``_reply``.
                return {}

            if binding_status == CHANNEL_BINDING_ACTIVE:
                ChannelInboundService._schedule(
                    ChannelInboundService._continue_thread(
                        binding_id=binding_id,
                        text=text,
                        # Plain uuids, like every other value crossing into
                        # this background task. The rows they name are already
                        # committed and ``temporary``; nothing ORM-shaped
                        # makes the hop.
                        file_ids=file_ids,
                        redelivered_file_ids=redelivered_file_ids,
                        external_message_id=inbound.external_message_id,
                        # Carried, not re-resolved — the same reasoning as the
                        # new-thread hop below. An existing identity thread is
                        # re-checked against ``allow_identity_routing`` on every
                        # message (see ``_ingest``), and it has to be checked
                        # against *this* message's reading of the sender's
                        # settings, which the decline gate above already made.
                        policy=policy,
                    ),
                    "channel_continue_thread",
                )
                # Silent ack: the agent's real reply arrives via the outbound
                # path, and a "working on it" here would double every turn.
                return {}

            if binding_status == CHANNEL_BINDING_PENDING_INSTALL:
                accepted = ChannelInboundService._park_message(
                    db, binding, inbound, text=text, file_ids=file_ids
                )
                # Tell the truth when the queue is full: "I'll answer shortly"
                # would be a promise about a message we just dropped.
                return adapter.build_sync_response(
                    REPLY_STILL_SETTING_UP if accepted else REPLY_TOO_MANY_QUEUED,
                    inbound.thread_key,
                )

            # `failed` — self-heal: drop the binding and route again from
            # scratch. A transient build failure must not wedge the thread.
            logger.info(
                "%s Clearing failed binding %s — re-routing", _LOG_PREFIX, binding_id
            )
            db.delete(binding)
            db.commit()

        # ---- 8-10. New thread: route (and possibly install) off-request ----
        #
        # **The classifier's input and the ingested text are two values now.**
        # They are the same string on every ordinary message, and deliberately
        # not on one: a sender who attached files and wrote nothing gave the
        # router no words at all, so it routes on the filenames instead of on
        # an empty string — see ``_attachment_classification_text``. The stored
        # content is never rewritten to contain that derived string; the
        # transcript records what the person actually sent.
        #
        # Computed here rather than at step 6.5 because it is only ever used
        # for a brand-new thread: an existing binding already names its agent,
        # so the read behind it would be wasted on every follow-up message.
        if inbound.has_sender_text:
            # ``has_sender_text`` for the same reason the rejection gate above
            # uses it: on email, ``inbound.text.strip()`` is truthy for a mail
            # the sender left entirely blank, so this arm used to hand the
            # router the forwarding wrapper — "--- Forwarded email content ---
            # From: someone@example.com … Attachments: - report.pdf" — as
            # though it were the sender's words. It classified on our own
            # scaffolding and never reached the filename-derived string below,
            # which is the only signal such a sender gave.
            #
            # ``inbound.text``, NOT the composed ``text``. The ⚠️ note is the
            # platform's prose about its own limits; feeding it to the router
            # would classify a message partly on what we said about it. The
            # note still reaches the transcript and the agent — it just is not
            # evidence of what the sender wanted.
            classification_text = inbound.text
        elif file_ids:
            classification_text = _attachment_classification_text(attachments)
        else:
            # No words and no files. Unreachable in practice (an adapter
            # returns ``ignored`` for an empty text-and-attachment-free event,
            # and the all-rejected case returned above), so this is the
            # defensive arm rather than a fourth behaviour.
            classification_text = text

        ChannelInboundService._schedule(
            ChannelInboundService._route_new_thread(
                channel_id=channel_id,
                user_id=user_id,
                # Carried, not re-resolved. The background task opens its own
                # session and could resolve again, and must not: the decline
                # gate above and the routing below have to be answering from
                # one reading of this person's settings, or a message declined
                # by one and routed by the other becomes a state nobody can
                # reproduce. Plain frozen data, so it survives the hop.
                policy=policy,
                thread_key=inbound.thread_key,
                text=text,
                classification_text=classification_text,
                file_ids=file_ids,
                redelivered_file_ids=redelivered_file_ids,
                external_message_id=inbound.external_message_id,
                # Bare platform id — `SessionSender.from_channel` adds the
                # `channel_type:` prefix, so namespacing here would double it.
                external_user_id=inbound.external_user_id,
                # Resolved from the adapter HERE, where one is in hand, rather
                # than from the channel row the background task reloads: same
                # answer, and it keeps the origin a fact about the transport
                # that accepted the message rather than about a row that could
                # have been edited in between.
                origin=_trace_origin(adapter.channel_type),
            ),
            "channel_route_new_thread",
        )
        if notice_supported and outbound_configured:
            # Ack in silence and let the background task own the narration.
            #
            # The sync response is the wrong vehicle for a notice that is going
            # to change: Chat creates the message but never tells us its id, so
            # a "finding an assistant…" answered here can be neither rewritten
            # when the answer is known nor removed when the reply lands — it is
            # a permanent message by construction. ``_route_new_thread`` posts
            # the same text through the API instead, keeps the id, and mutates
            # that one message the rest of the way.
            #
            # Both booleans were resolved in the hoist above step 6.5, before
            # ``materialize``'s commit expired ``channel``; see it for why.
            #
            # ``notice_supported`` comes from ``_status_notice_supported``
            # rather than off ``adapter.capabilities`` directly, because every
            # other reader in this file goes through it and the two can
            # disagree: the helper is total and answers False for a channel
            # whose adapter no longer resolves, so an ungated read here could
            # ack in silence for a transport the *notice* path had already
            # decided cannot narrate. Silence plus no notice is the one
            # combination with no observable difference from the message never
            # arriving.
            #
            # The credential check guards the same outcome by the other route.
            # This module's own docstring makes a point of the sync reply
            # needing no outbound credential — "which is what lets a channel
            # refuse a sender before setup is finished" — and moving the
            # narration into a posted message quietly narrowed that property to
            # declines only. A channel whose inbound verification works but
            # whose outbound does not (key rotated away, egress blocked, app
            # removed from the space) would answer an ACCEPTED sender with
            # nothing at all, traced only by a ring-buffer entry that is gone
            # on the next restart. When the credential is provably absent the
            # notice provably cannot be posted, so the old sync ack is strictly
            # better than silence.
            return {}
        # Transports that cannot run a notice — and channels that could but have
        # nothing to post it with — keep the sync reply, which is still the
        # fastest acknowledgement available to them.
        return adapter.build_sync_response(REPLY_WORKING, inbound.thread_key)

    # ==================================================================
    # Step 5 — user resolution
    # ==================================================================

    @staticmethod
    async def _resolve_user(
        *,
        db: DBSession,
        channel: ServerChannel,
        inbound: ChannelInboundMessage,
    ) -> User | None:
        """Find (or auto-create) the sender's platform account.

        Returns None for every denial case — an inactive account, or an unknown
        sender on a channel that has auto-registration off. The caller sends the
        same reply either way so the difference is not observable.
        """
        email = (inbound.sender_email or "").strip().lower()
        user = UserService.get_user_by_email(session=db, email=email)

        if user is not None:
            if not user.is_active:
                logger.info(
                    "%s Denying inactive user %s on channel %s",
                    _LOG_PREFIX,
                    user.id,
                    channel.id,
                )
                return None
            return user

        if not channel.auto_register_users:
            logger.info(
                "%s Unknown sender on channel %s and auto-register is off",
                _LOG_PREFIX,
                channel.id,
            )
            return None

        # The transport verified this address (Google signed the payload), so
        # the account is created confirmed and passwordless — same justification
        # as Google OAuth signup. The channel whitelist is the sole registration
        # gate; AUTH_WHITELIST_USER_DOMAINS is deliberately not re-checked.
        try:
            user = UserService.create_external_user(
                session=db,
                email=email,
                confirmed=True,
                provenance=f"server_channel:{channel.id}",
                passwordless=True,
            )
        except ValueError:
            logger.warning(
                "%s Rejected malformed sender address on channel %s",
                _LOG_PREFIX,
                channel.id,
            )
            return None

        await ChannelInboundService._audit(
            db=db,
            user_id=user.id,
            event_type=security_event_constants.SERVER_CHANNEL_USER_AUTO_REGISTERED,
            severity="medium",
            details={
                "server_channel_id": str(channel.id),
                "channel_type": channel.channel_type,
                "email": email,
            },
        )
        logger.info(
            "%s Auto-registered user %s from channel %s",
            _LOG_PREFIX,
            user.id,
            channel.id,
        )
        return user

    # ==================================================================
    # Step 6 — continue an existing thread
    # ==================================================================

    @staticmethod
    async def _continue_thread(
        *,
        binding_id: uuid.UUID,
        text: str,
        policy: ResolvedChannelPolicy,
        external_message_id: str | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Feed a message into the session an active binding already owns.

        ``file_ids`` are the sender's attachments, already materialised into
        ``FileUpload`` rows at step 6.5 and travelling beside ``text`` from
        here down — including through a park, if the drain ahead of this
        message fails and it has to go to the back of the queue.

        ``policy`` is ``process_inbound``'s resolution, carried in rather than
        re-read here. **One reading per message, and on this path the carried
        one is the only one** — the decline gate that let the message through
        and the identity-consent check in ``_ingest`` are answering from the
        same frozen value, so they cannot disagree about a channel edited
        mid-flight. (The scheduler's drain path has no live resolution to
        carry, so ``_flush_one`` makes its own, and that one is likewise the
        only reading for the messages it delivers.)
        """
        from app.core.db import create_session

        with create_session() as db:
            binding = db.get(ChannelThreadBinding, binding_id)
            if binding is None:
                return
            channel = db.get(ServerChannel, binding.server_channel_id)
            agent = db.get(Agent, binding.agent_id)
            user = db.get(User, binding.user_id)
            if channel is None or agent is None or user is None:
                return

            # Any messages left parked by an interrupted drain go first, so the
            # conversation stays in order.
            if binding.pending_messages:
                await ChannelInboundService._drain_parked(
                    db=db,
                    channel=channel,
                    binding=binding,
                    agent=agent,
                    user=user,
                    policy=policy,
                )
                # `_drain_parked` stops on the first failure and leaves the rest
                # parked. Ingesting the new message now would let it overtake
                # older ones the person is still waiting on — the exact
                # out-of-order delivery the drain is written to prevent. Park it
                # at the back of the queue instead; the next inbound message (or
                # a later drain) retries the whole queue in order.
                db.refresh(binding)
                if binding.pending_messages:
                    accepted = ChannelInboundService._append_parked(
                        db, binding, text, external_message_id, None, file_ids
                    )
                    binding.updated_at = datetime.now(UTC)
                    db.add(binding)
                    db.commit()
                    # Same invariant `_park_message` holds: at the cap the
                    # message is gone, so say so. The failed drain already sent
                    # a generic setup-failed notice, but that does not tell the
                    # sender THIS message was dropped — and a silent drop here
                    # leaves them waiting for an answer that can never come.
                    if not accepted:
                        await ChannelInboundService._reply(
                            db, channel, binding.thread_key, REPLY_TOO_MANY_QUEUED
                        )
                    return

            # A bound thread used to say nothing at all while it worked — the
            # webhook acked in silence and the next thing the person saw was
            # the answer, however long that took. It can say something now
            # because it can take it back: the notice is deleted the moment the
            # reply lands (``handle_stream_completed``), so the thread ends up
            # exactly as quiet as before.
            #
            # Gated on the notice capability rather than on
            # ``supports_progress_updates``: a transport that can post but not
            # delete would leave one of these standing on every single turn,
            # which is worse than the silence it replaced.
            if _status_notice_supported(channel):
                await ChannelOutboundService.set_binding_status(
                    db=db, channel=channel, binding=binding, text=REPLY_WORKING_ON_IT
                )

            delivered = await ChannelInboundService._ingest_or_fail(
                db=db,
                channel=channel,
                binding=binding,
                agent=agent,
                user=user,
                text=text,
                file_ids=file_ids,
                redelivered_file_ids=redelivered_file_ids,
                external_message_id=external_message_id,
                external_user_id=None,
                policy=policy,
            )

            # Record the delivered id only now. Stamping it at webhook time
            # would dedup a redelivery of a message we then failed to process,
            # losing it silently.
            #
            # **``delivered`` is the load-bearing term**, and it is the one
            # that used to be missing. The other two conditions describe the
            # binding, not the message: they were satisfied by an ingest that
            # returned a soft ``action="error"`` and created nothing, and the
            # stamp that followed made every redelivery of that message dedup
            # against a delivery that never happened. That is silent,
            # permanent loss — and it needs no attachment to reproduce, since
            # the message commit and this stamp are two separate commits and a
            # crash between them lands in the same place.
            #
            # ``binding.status`` is kept beside it rather than replaced: it
            # catches a binding failed by something other than this call, and
            # the two are cheap.
            if (
                delivered
                and external_message_id
                and binding.status == CHANNEL_BINDING_ACTIVE
                and binding.last_external_message_id != external_message_id
            ):
                binding.last_external_message_id = external_message_id
                db.add(binding)
                db.commit()

    # ==================================================================
    # Steps 8-10 — routing for a brand-new thread
    # ==================================================================

    @staticmethod
    async def _route_new_thread(
        *,
        channel_id: uuid.UUID,
        user_id: uuid.UUID,
        policy: ResolvedChannelPolicy,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        origin: str,
        classification_text: str | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """``decide()`` → bind → ingest.

        The decision itself — Pass 1 over the sender's installed agents, then
        Pass 2 over the auto-install catalog — lives in
        :meth:`ChannelRoutingService.decide` and has no effects of its own. What
        remains here is everything that *does* have effects: the thread binding,
        the session ingest, the install-and-park, the outbound reply, the debug
        feed entry, and the durable trace write.

        That split is what makes ``POST /admin/routing/simulate`` safe by
        construction: simulate calls ``decide`` and stops, so there is no
        ``simulate`` flag in this method to get a branch wrong about.

        ``policy`` is the resolution ``process_inbound`` already made — the same
        one its decline gate consulted — carried in rather than resolved again
        here. It is a frozen dataclass of scalars, so it crosses into this
        background task the way ids and text do, and this method's
        freshly-opened session is never asked to re-derive an inherit rule.

        ``origin`` travels onto the trace unchanged, carried in for the same
        reason ``policy`` is: it is a plain string resolved from the adapter
        that accepted the message, and re-deriving it from the reloaded channel
        row would make it a fact about the row's current state instead.

        **``classification_text`` and ``text`` are two parameters on purpose.**
        ``decide()`` gets the first; everything that persists — the ingest, the
        park — gets the second. Today they hold the same string on every
        ordinary message, and the temptation to "tidy" them back into one is
        exactly what this docstring exists to refuse: an attachment-only
        message has no words for the router, so its classification text is
        derived from the accepted filenames (plan §5.4) while its stored
        content stays what the sender actually sent. ``None`` means "they are
        the same", which keeps every existing caller honest without a second
        argument.

        ``file_ids`` are the sender's attachments, materialised at step 6.5 and
        carried down whichever branch this message takes — ingested with it,
        parked with it behind an auto-install, or handed to the winner of a
        binding race along with the text.
        """
        # Resolved once, here, so the ``decide()`` call below reads as the one
        # thing it is rather than as a conditional buried in an argument list.
        if classification_text is None:
            classification_text = text

        from app.core.db import create_session

        with create_session() as db:
            channel = db.get(ServerChannel, channel_id)
            user = db.get(User, user_id)
            if channel is None or user is None:
                return

            # Every scalar the diagnostic records below need, read HERE — while
            # both instances are freshly loaded, and before the first
            # ``db.commit()``. §11a Rule 2: the test for an instrumentation
            # point is not "is the recorder guarded" but "can anything in the
            # caller's argument list raise".
            #
            # Hoisting these to just above their ``ChannelDebugBuffer.record``
            # call would not have been enough. That commit expires every
            # instance in this session, so ``channel.id`` / ``user.email`` are
            # lazy reloads wherever they are read afterwards, and a reload of a
            # row deleted concurrently raises ``ObjectDeletedError`` into the
            # broad ``except`` at the bottom of this method — REPLY_SETUP_FAILED
            # to a sender whose message routed perfectly. Moving the read a few
            # lines up its own block only relocates that raise inside the same
            # handler. Read before the commit and there is no reload at all:
            # from here on these are plain Python values that cannot touch the
            # database, whatever happens to the session or to the rows.
            debug_channel_id = channel.id
            sender_email = user.email

            # Read with the two above, and for the third reason as well: the
            # notice below runs on a detached ``channel``, so an adapter lookup
            # off a live instance has to happen here or not at all.
            notice_supported = _status_notice_supported(channel)

            # Opened before the slow part, because the slow part is what it
            # exists to narrate: ``decide()`` is an LLM call, and an install
            # behind it is minutes. Posted through the API rather than answered
            # synchronously so the id comes back — every state below rewrites
            # THIS message rather than adding another one.
            #
            # A plain local, carried down the branches exactly as ``policy`` and
            # ``origin`` are, until there is a binding to hand it to. It cannot
            # live on the binding yet: routing is what decides whether there
            # will be one.
            status_message_id: str | None = None
            # The binding that has TAKEN OVER the notice, once one exists.
            #
            # The id lives in exactly one place at a time, and this is what
            # keeps that true. Adopting it onto a binding used to leave a
            # second copy in ``status_message_id``, and the failure handler at
            # the bottom of this method could only settle *that* one: the
            # binding row kept the id, and 45 seconds later the flush loop
            # found it and patched "💬 Your assistant is ready — working on
            # your message…" straight over the "setup failed" the sender had
            # just been shown. Every adopt below therefore clears the local and
            # records the owner here instead, and the handler settles through
            # whichever of the two actually holds it.
            bound: ChannelThreadBinding | None = None
            if notice_supported:
                # ---- Release the outer connection, THEN narrate ----
                #
                # The status notice is an HTTP call — up to ``_SEND_ATTEMPTS``
                # requests at a 30s timeout each, plus backoff — and it used to
                # go out *below* this commit, with the transaction the two
                # ``db.get`` calls opened still held. That is precisely what
                # the comment inside this block forbids, one statement too
                # late: a Chat API slowdown pinned one pooled connection per
                # inbound new thread.
                #
                # Committing alone does not fix it. ``commit`` expires both
                # instances, so the notice's first attribute read
                # (``channel.channel_type``, then ``encrypted_secrets``) is a
                # lazy reload that opens a *new* transaction and holds that
                # connection for the whole HTTP call — the same pin, moved. So
                # both instances are expunged first: detached and fully loaded,
                # they answer the adapter's column reads out of memory and
                # touch no connection at all. They are re-fetched below, once
                # there is DB work to do again.
                #
                # All of it lives INSIDE the ``notice_supported`` gate: a
                # transport with no status notice (email, App MCP) does nothing
                # between the expunge and the re-fetch, so it was paying a
                # commit and two extra ``SELECT``s per new thread to protect a
                # window in which nothing happens. The ``db.commit()`` that
                # releases the connection for ``decide()`` is the first
                # statement of the ``try`` below and covers those transports.
                db.expunge(channel)
                db.expunge(user)
                db.commit()

                status_message_id = await ChannelOutboundService.set_status(
                    channel=channel,
                    thread_key=thread_key,
                    message_id=None,
                    text=REPLY_WORKING,
                )
                if status_message_id is None:
                    # The transport declares it can run a notice and the post
                    # failed anyway. That matters more than a failed notice
                    # normally would: ``handle_inbound`` acks such a channel in
                    # SILENCE, on the strength of this message being the
                    # sender's acknowledgement, so unless the credential gate
                    # there sent the synchronous fallback instead this turn has
                    # reached the sender with nothing at all. The only other
                    # trace is a ``DEBUG_SEND_FAILED`` row in a process-local
                    # ring buffer, which is empty again after a restart — hence
                    # a log line rather than leaving it to the debug panel.
                    logger.warning(
                        "%s Status notice could not be posted for channel %s "
                        "thread %s — this turn may have reached the sender "
                        "with no acknowledgement at all; check the channel's "
                        "outbound credential and egress",
                        _LOG_PREFIX,
                        channel_id,
                        thread_key,
                    )

                # Re-attached now the network work is done: everything below
                # writes through this session, and a detached instance handed
                # to ``_upsert_binding`` or ``_ingest_or_fail`` would be a
                # defect waiting on the first relationship access.
                #
                # The detached one is KEPT rather than overwritten. It is fully
                # loaded — outbound credentials included — so it can still
                # address the notice we just posted even when the row it came
                # from is gone; reassigning ``channel`` first threw that away
                # and left the sender on "finding an assistant…" forever, with
                # nothing in the process able to settle it.
                notice_channel = channel
                channel = db.get(ServerChannel, channel_id)
                user = db.get(User, user_id)
                if channel is None or user is None:
                    # Deleted while the notice was in flight. Nothing left to
                    # route to — but the notice is ours and is still narrating,
                    # so it gets the last word out of the detached instance
                    # rather than being abandoned mid-sentence.
                    if status_message_id:
                        await ChannelInboundService._settle_notice(
                            db,
                            notice_channel,
                            thread_key,
                            status_message_id,
                            REPLY_SETUP_FAILED,
                        )
                    return

            try:
                # ---- Decide ----
                # Release the outer connection for the whole decision. The
                # worker threads open their own, and the pool is 15 wide
                # against a 40-thread limiter — holding one idle-in-transaction
                # here would let a burst of new threads stall unrelated
                # requests behind QueuePool timeouts. Nothing below depends on
                # outer-transaction continuity across the offload.
                #
                # One commit now covers both passes, where there used to be one
                # per pass: ``decide`` holds no session of ours, so between Pass
                # 1 and Pass 2 this session touches nothing and its connection
                # stays back in the pool for the whole cascade rather than being
                # re-taken for the interleaved ``db.get``.
                db.commit()
                # ``user_id`` / ``channel_id``, not ``user.id`` / ``channel.id``:
                # same values, but this method already holds them as plain
                # parameters, so reading them off the just-expired instances was
                # a database round trip that could raise — for nothing.
                decision = await ChannelRoutingService.decide(
                    user_id=user_id,
                    # NOT ``text`` — see the docstring. The router classifies
                    # what the sender meant; ``text`` is what gets stored.
                    text=classification_text,
                    policy=policy,
                    channel_id=channel_id,
                    thread_key=thread_key,
                    origin=origin,
                )
                pass1_trace = decision.pass1_trace
                pass2_trace = decision.pass2_trace
                agent = db.get(Agent, decision.agent_id) if decision.agent_id else None
                if agent is not None:
                    # ``agent`` was just loaded by the ``db.get`` above, so
                    # these two are in-memory reads rather than the lazy
                    # reloads ``channel``/``user`` would be — but they are
                    # hoisted anyway, because §11a Rule 2 is a rule about the
                    # *shape* of an argument list, not about which reads happen
                    # to be safe today. An earlier version hoisted ``agent.name``
                    # alone, with a comment explaining exactly this hazard, and
                    # left ``channel.id`` / ``user.email`` / ``agent.id`` inline
                    # — which is why the rule is written as "sweep the pattern",
                    # not "guard the one that bit".
                    agent_name = agent.name
                    agent_ref = str(agent.id)
                    # Pass 1 was terminal, so this trace is the whole decision —
                    # ``persist_args`` says so rather than this call site
                    # deciding it a second time (simulate persists the same
                    # decision through the same rule; two call sites applying it
                    # by hand is how the admin list ends up comparing rows of
                    # different shapes).
                    #
                    # Offloaded, like the routing passes above. ``persist`` is
                    # synchronous and does an INSERT+COMMIT; run inline it does
                    # that ON THE EVENT LOOP, stalling every other request and
                    # stream in the process for the duration of the round trip.
                    # (It takes a second pooled connection either way — a worker
                    # thread needs one too. What the offload buys is that the
                    # loop is not the thing waiting on it.)
                    decision_id = await ChannelRoutingService.run_in_thread(
                        decision.persist_call()
                    )
                    ChannelDebugBuffer.record(
                        channel_id=debug_channel_id,
                        direction="inbound",
                        kind=DEBUG_ROUTED,
                        # Two sentences, because "installed agent" is false on
                        # the identity branch — that agent is someone else's,
                        # and the single most useful thing this line can say
                        # about such a message is that it left the sender's own
                        # workspace. Both operands are plain reads (a local
                        # string, a frozen-dataclass attribute), so this
                        # argument list still cannot raise (§11a Rule 2).
                        summary=(
                            f"Routed via identity to '{agent_name}' in another "
                            "user's workspace"
                            if decision.identity_grant is not None
                            else f"Routed to installed agent '{agent_name}'"
                        ),
                        sender_email=sender_email,
                        thread_key=thread_key,
                        detail={
                            "pass": "1",
                            "agent_id": agent_ref,
                            **_decision_detail(decision_id),
                        },
                    )
                    binding, created = ChannelInboundService._upsert_binding(
                        db=db,
                        channel=channel,
                        user=user,
                        agent=agent,
                        thread_key=thread_key,
                        status=CHANNEL_BINDING_ACTIVE,
                        external_message_id=external_message_id,
                    )
                    if not created:
                        # Another delivery for this brand-new thread won the
                        # race and already picked an agent. Defer to its
                        # binding — continuing with our own would create a
                        # session on a different agent than the binding names.
                        #
                        # Our notice goes with our routing result — handed
                        # down so the refusal is written INTO it rather than
                        # posted under it. The winner posted a notice of its
                        # own and the binding points at that one; leaving ours
                        # behind would strand a second "finding an assistant…"
                        # on the thread that nothing owns and nothing rewrites.
                        await ChannelInboundService._handle_lost_race(
                            db=db,
                            channel=channel,
                            binding=binding,
                            sender_user_id=user.id,
                            thread_key=thread_key,
                            text=text,
                            file_ids=file_ids,
                            external_message_id=external_message_id,
                            external_user_id=external_user_id,
                            policy=policy,
                            status_message_id=status_message_id,
                            redelivered_file_ids=redelivered_file_ids,
                        )
                        return
                    # The thread has a binding now, so the notice gets an owner
                    # — and its last state before the answer: "working on it".
                    had_notice = status_message_id is not None
                    ChannelOutboundService.adopt_status_notice(
                        db, binding, status_message_id
                    )
                    # Recorded IMMEDIATELY after the adopt, and above the
                    # announcement rather than below it. Ownership transfers at
                    # the adopt — the id is on the row from that statement on —
                    # so the bookkeeping has to transfer there too. It used to
                    # sit under ``set_binding_status``, which is a full network
                    # round trip and is NOT never-raise (see ``set_status``: the
                    # adapter lookup is a lazy reload on an expired instance).
                    # A raise inside that window left ``bound`` at ``None``
                    # while the row already held the id, so the handler at the
                    # bottom settled the stale local and the flush loop found
                    # the row's id minutes later and patched "ready" over
                    # whatever the sender had last been shown.
                    # See ``bound`` above for why the local is cleared rather
                    # than left as a second copy.
                    bound = binding
                    status_message_id = None
                    if had_notice:
                        await ChannelOutboundService.set_binding_status(
                            db=db,
                            channel=channel,
                            binding=binding,
                            text=REPLY_WORKING_ON_IT,
                        )
                    await ChannelInboundService._ingest_or_fail(
                        db=db,
                        channel=channel,
                        binding=binding,
                        agent=agent,
                        user=user,
                        text=text,
                        file_ids=file_ids,
                        external_message_id=external_message_id,
                        external_user_id=external_user_id,
                        # Set only when Stage 1 chose a *person* and Stage 2
                        # picked one of their agents (plan §2.3). It is what
                        # permits a session on an agent this sender does not
                        # own — and it is a claim, re-read in full by
                        # ``assert_access`` before anything is created.
                        # ``None`` on every other branch, which leaves the
                        # channel invariant exactly as strict as it was.
                        identity_grant=decision.identity_grant,
                        policy=policy,
                        redelivered_file_ids=redelivered_file_ids,
                    )
                    return

                # ---- Pass 2 outcome: server-wide auto-install catalog ----
                # Already run inside ``decide`` above; from here on this method
                # only turns its verdict into effects.
                bundle_uuid = decision.bundle_uuid
                bundle = db.get(AgentBundle, bundle_uuid) if bundle_uuid else None
                bundle_display_name = bundle.display_name if bundle is not None else ""
                bundle_id = bundle.id if bundle is not None else None
                # ``debug_channel_id`` / ``sender_email`` come from the top of
                # this method, read before the first commit. Found by the Rule 2
                # sweep: this block hoisted its *bundle* reads but left
                # ``channel.id`` and ``user.email`` inline in both ``record``
                # calls below, exactly as Pass 1 did — two adjacent blocks
                # contradicting each other on the same question.
                #
                # **Persisted AFTER the effect it describes, in every branch.**
                # The single ``persist`` used to sit here, above both branches —
                # so a trace already settled as ``parked_install`` (Pass 2's
                # classifier settles it the moment it picks a bundle) became a
                # durable row claiming an install that had not been attempted
                # yet, and might never be: the bundle can vanish between the
                # classifier's pick and the ``db.get`` above, and
                # ``_install_and_park`` can fail outright. Either way the row
                # read ``outcome=parked_install, error=NULL`` — invisible to
                # ``?outcome=error``, the one filter that exists to find it.
                # Same defect class as the omitted Pass-2 stage: the trace
                # asserting something about execution that did not happen.
                #
                # Deliberately NOT solved by writing a provisional row and
                # correcting it — a corrected row is worse than a late one, and
                # the correction is the write most likely to be the one that
                # fails. Each branch persists once, after it knows what really
                # happened, and the ``ChannelDebugBuffer`` record follows the
                # persist because it is the ``decision_id`` link that wanted the
                # early write, not the buffer entry.
                if bundle is None:
                    if decision.agent_id is not None and pass1_trace is not None:
                        # Pass 1 picked an agent that vanished before we could
                        # bind it — the mirror of the bundle case below, and the
                        # ONLY way control reaches here with an agent selected.
                        #
                        # Without this the row is the worst shape this table can
                        # hold: ``outcome=routed, selected_agent_id=<deleted>,
                        # error=NULL`` — invisible to ``?outcome=error`` AND to
                        # ``?outcome=no_match``, while the sender was told no
                        # match and the live feed logged one. A durable record
                        # contradicting both the reply and the feed, and looking
                        # like the most ordinary success the table has. §11a
                        # Rule 1, on the write path.
                        #
                        # It arrived with the ``decide()`` split: Pass 2 is now
                        # gated on Pass 1 returning no agent, so the vanished
                        # agent no longer falls through to a Pass 2 whose own
                        # trace would have supplied the verdict. ``record_error``
                        # settles the trace as ``error`` and clears the stale
                        # selection through the same settler the bundle branch
                        # uses. ``decision.agent_id`` is a plain frozen-dataclass
                        # attribute, so this argument list cannot raise.
                        pass1_trace.record_error(
                            f"selected agent {decision.agent_id} no longer exists"
                        )
                    if bundle_uuid is not None and pass2_trace is not None:
                        # The classifier picked a bundle that no longer exists —
                        # a data-integrity failure between the pick and this
                        # read, not "nothing matched". Recorded so the row
                        # settles as ``error`` instead of keeping a
                        # ``parked_install`` verdict for an install nobody
                        # attempted. ``bundle_uuid`` is a plain value returned by
                        # the thread target, so this argument cannot raise.
                        pass2_trace.record_error(
                            f"selected bundle {bundle_uuid} no longer exists"
                        )
                    # ONE row for the whole decision: Pass 1's stages are folded
                    # in rather than written as their own ``no_match`` row, which
                    # would otherwise appear on the ``?outcome=no_match`` filter
                    # for every message Pass 2 went on to handle.
                    decision_id = await ChannelRoutingService.run_in_thread(
                        decision.persist_call()
                    )
                    # "Nothing matched" is the least actionable line the panel
                    # can show. `summarize` turns both passes into one sentence
                    # naming the candidates and why each was dropped; it never
                    # raises and returns "" when it has nothing to add.
                    diagnosis = routing_trace.summarize(pass1_trace, pass2_trace)
                    # Both hoisted to plain locals before the record call, and
                    # both describe what actually happened rather than the
                    # common case. ``pass`` used to be the literal "2" here,
                    # which since the ``decide()`` split can be a pass that
                    # never ran; and the vanished-agent branch above reaches
                    # this record having selected something, so "nothing
                    # matched" would be flatly untrue.
                    reached_pass = "2" if decision.catalog_ran else "1"
                    if decision.agent_id is not None:
                        headline = (
                            "Routing picked an agent that no longer exists — "
                            "nothing to bind"
                        )
                    else:
                        headline = (
                            "No installed agent and no auto-install catalog "
                            "candidate matched this message"
                        )
                    ChannelDebugBuffer.record(
                        channel_id=debug_channel_id,
                        direction="inbound",
                        kind=DEBUG_NO_MATCH,
                        summary=headline + (f" — {diagnosis}" if diagnosis else ""),
                        sender_email=sender_email,
                        thread_key=thread_key,
                        detail={
                            "pass": reached_pass,
                            **_decision_detail(decision_id),
                        },
                    )
                    # Settles the notice rather than clearing it: "nothing
                    # matched" IS the answer, so it takes the notice's place
                    # instead of being posted under it. Nothing owns the id
                    # afterwards — there is no binding, and the message is
                    # meant to stay.
                    await ChannelInboundService._settle_notice(
                        db, channel, thread_key, status_message_id, REPLY_NO_MATCH
                    )
                    return

                try:
                    bound = await ChannelInboundService._install_and_park(
                        db=db,
                        channel=channel,
                        user=user,
                        bundle=bundle,
                        thread_key=thread_key,
                        text=text,
                        file_ids=file_ids,
                        external_message_id=external_message_id,
                        external_user_id=external_user_id,
                        policy=policy,
                        status_message_id=status_message_id,
                        # Only ever consumed on the lost-race branch, which
                        # ingests through the winner's binding. The park branch
                        # cannot need it: a ``pending_install`` binding has no
                        # session, so nothing it parks was ever attached.
                        redelivered_file_ids=redelivered_file_ids,
                    )
                except Exception as exc:
                    # The park is what ``parked_install`` asserts, so a failed
                    # park must not persist as one. Recorded and written here
                    # rather than left to the handler below, which knows nothing
                    # about traces; then re-raised unchanged so the sender still
                    # gets REPLY_SETUP_FAILED exactly as before.
                    if pass2_trace is not None:
                        pass2_trace.record_error(exc)
                    await ChannelRoutingService.run_in_thread(decision.persist_call())
                    raise
                # Either the binding now owns the notice (``bound``) or the
                # lost-race path already settled it into a refusal. Neither
                # leaves anything for this method to write into.
                status_message_id = None

                decision_id = await ChannelRoutingService.run_in_thread(
                    decision.persist_call()
                )
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="inbound",
                    kind=DEBUG_INSTALLING,
                    summary=f"Auto-installing '{bundle_display_name}' — message parked",
                    sender_email=sender_email,
                    thread_key=thread_key,
                    detail={
                        "pass": "2",
                        "bundle_uuid": str(bundle_id),
                        **_decision_detail(decision_id),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Routing failed for channel %s thread %s",
                    _LOG_PREFIX,
                    channel_id,
                    thread_key,
                )
                # Both branches below bottom out in ``set_status``, and this
                # handler is the one place its totality is not a nicety. The
                # exception being handled is often a DB error, which leaves
                # ``channel`` expired AND the session poisoned — so the
                # adapter lookup's ``channel.channel_type`` read is a lazy
                # reload that raises ``PendingRollbackError``. If that escaped,
                # it would escape the handler itself and the sender's notice
                # would be stranded on "Setting up…" forever, with no further
                # code path able to touch it. ``set_status`` catches every
                # exception for exactly this reason.
                if bound is not None:
                    # Settled THROUGH the binding, which is where the id lives
                    # once it has been adopted. ``settle=True`` also releases
                    # it — but only if the write landed — and that release is
                    # the point: an id left on the row is one the pending-flush
                    # loop will find minutes later and patch "ready — working on
                    # your message…" over the failure the sender was just shown.
                    # If the settle write failed there is no such failure on
                    # screen to protect, so the id is kept on purpose and the
                    # next write patches the standing notice.
                    await ChannelOutboundService.set_binding_status(
                        db=db,
                        channel=channel,
                        binding=bound,
                        text=REPLY_SETUP_FAILED,
                        settle=True,
                    )
                else:
                    await ChannelInboundService._settle_notice(
                        db, channel, thread_key, status_message_id, REPLY_SETUP_FAILED
                    )

    @staticmethod
    async def _settle_notice(
        db: DBSession,
        channel: ServerChannel,
        thread_key: str,
        status_message_id: str | None,
        text: str,
    ) -> None:
        """Write the last word into an unbound thread's status notice.

        The thread-keyed twin of ``set_binding_status(settle=True)``, for the
        two outcomes that end a new thread before it ever gets a binding: no
        agent matched, and routing failed outright. Both are the reply, not a
        state on the way to one, so the notice is rewritten and left standing.

        Falls back to :meth:`_reply` when there is no notice to rewrite — which
        is every transport that cannot mutate its own messages, and any thread
        whose opening notice failed to post. Same text, same thread, one extra
        message.

        Also used by :meth:`_handle_lost_race` to dispose of the *loser's*
        orphan notice, which is the same operation seen from the other side:
        the refusal is written into the message that was narrating, rather than
        posted under it and the notice deleted.
        """
        if status_message_id:
            await ChannelOutboundService.set_status(
                channel=channel,
                thread_key=thread_key,
                message_id=status_message_id,
                text=text,
            )
            return
        await ChannelInboundService._reply(db, channel, thread_key, text)

    @staticmethod
    async def _handle_lost_race(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        sender_user_id: uuid.UUID,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        policy: ResolvedChannelPolicy,
        status_message_id: str | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Deliver this message via the binding that won the creation race.

        ``file_ids`` simply follow ``text`` through all three branches below —
        the loser's message is delivered through the winner's binding *with its
        files*, parked with them, or refused along with them. The semantics are
        unchanged; the attachments are not a separate decision.

        ``status_message_id`` is the **loser's** own status notice, which is
        now an orphan: the thread's notice is whichever one the winner's
        binding points at. Both refusal branches below settle their text into
        it, which disposes of it and answers the sender in one message. The
        third branch — delivering through the winner's binding — has no text
        for it and deletes it, one of the two places that is the right move.

        Never proceeds with the loser's own routing result: the winner's
        binding already names an agent, and creating a session on a different
        one would leave the binding permanently lying about who is answering.

        The same one-person-per-thread rule the synchronous path enforces
        applies here, and it must be re-checked rather than assumed. Two
        members of a group space can @-mention the bot in the same new thread;
        both see no binding, both run a full routing pass — the window is LLM
        latency, seconds — and both try to create it. Without this check the
        loser's message would be ingested into (or parked onto) the *winner's*
        session, putting one external user's text in another's history. Since
        this runs in a background task, the refusal is delivered as a message
        rather than a sync reply.
        """
        if binding.user_id != sender_user_id:
            logger.warning(
                "%s User %s lost the binding race for thread %s to user %s — "
                "declining rather than delivering into their session",
                _LOG_PREFIX,
                sender_user_id,
                thread_key,
                binding.user_id,
            )
            await ChannelInboundService._settle_notice(
                db, channel, thread_key, status_message_id, REPLY_THREAD_OWNED
            )
            return

        if binding.status == CHANNEL_BINDING_PENDING_INSTALL:
            accepted = ChannelInboundService._append_parked(
                db, binding, text, external_message_id, external_user_id, file_ids
            )
            db.add(binding)
            db.commit()
            # Never drop in silence: at the cap the message is gone, so say so
            # rather than leaving the sender waiting for an answer to it.
            await ChannelInboundService._settle_notice(
                db,
                channel,
                thread_key,
                status_message_id,
                REPLY_STILL_SETTING_UP if accepted else REPLY_TOO_MANY_QUEUED,
            )
            return

        # Delivering through the winner's binding: this notice has nothing left
        # to say and no thread state of its own, so it goes. See
        # ``ChannelOutboundService.clear_status`` for why deletion is the
        # exception rather than the rule.
        await ChannelOutboundService.clear_status(
            channel=channel, thread_key=thread_key, message_id=status_message_id
        )
        agent = db.get(Agent, binding.agent_id)
        user = db.get(User, binding.user_id)
        if agent is None or user is None:
            return
        await ChannelInboundService._ingest_or_fail(
            db=db,
            channel=channel,
            binding=binding,
            agent=agent,
            user=user,
            text=text,
            file_ids=file_ids,
            external_message_id=external_message_id,
            external_user_id=external_user_id,
            # The loser's own reading, which is the reading for this message —
            # the winner's binding decides which agent answers, never whose
            # consent applies.
            policy=policy,
            redelivered_file_ids=redelivered_file_ids,
        )

    @staticmethod
    async def _ingest_or_fail(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        policy: ResolvedChannelPolicy,
        identity_grant: IdentityGrant | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> bool:
        """Ingest, leaving the binding in a coherent state on every outcome.

        **Returns whether a message was actually created**, and that return
        value is not decoration. Callers used to read "did an exception escape
        this call" as "was the message delivered", which is a different
        question and was wrong in exactly the case that matters: a soft
        ``action="error"`` from the ingestion service returned normally, the
        caller stamped ``binding.last_external_message_id`` as though the
        message had landed, and the sender's message was lost permanently —
        the stamp dedups every redelivery of it from then on.
        ``_ingest`` now raises ``ChannelIngestProducedNoMessage`` for that
        family, and this returns ``False`` for it, so a caller can gate on the
        answer rather than on the absence of a symptom.

        Without this, a Pass 1 ingest failure would leave an ``active`` binding
        with no session and no error — a state the self-heal path (which only
        triggers on ``failed``) never cleans up.

        ``identity_grant`` is forwarded verbatim and defaults to ``None``: only
        the first message of a freshly-routed thread has a routing decision
        behind it. Every later message reconstructs its own grant from the
        session row (see :meth:`_ingest`), because a stale claim carried across
        turns is exactly what re-verification exists to prevent.

        ``policy`` is required, not defaulted: it is the sender's reading for
        *this* message and there is no safe value to invent for a caller that
        forgot it — a permissive default would silently re-open identity
        routing for a person who has switched it off.

        ``file_ids`` are forwarded verbatim. A failure to copy them into the
        agent's container surfaces as the ``MessageServiceError`` the broad
        handler below already turns into a failed binding and the generic
        setup-failed reply, which self-heals on the next message — the same
        disposition every other ingest failure gets.

        ``redelivered_file_ids`` is forwarded verbatim too; see
        :meth:`_ingest`.
        """
        try:
            await ChannelInboundService._ingest(
                db=db,
                channel=channel,
                binding=binding,
                agent=agent,
                user=user,
                text=text,
                file_ids=file_ids,
                redelivered_file_ids=redelivered_file_ids,
                sender_external_id=external_user_id,
                identity_grant=identity_grant,
                policy=policy,
            )
            return True
        except NoActiveEnvironmentError:
            # Terminal, not transient: `SessionService.create_session` returns
            # None only when the agent has NO `active_environment_id` at all
            # (a suspended or still-building environment succeeds and is woken
            # by `initiate_stream`). Nothing will fix that on its own, so fail
            # rather than parking a message behind a wait that never ends. The
            # binding self-heals — the next inbound message deletes it and
            # re-routes.
            logger.info(
                "%s Agent %s has no active environment — failing binding %s",
                _LOG_PREFIX,
                agent.id,
                binding.id,
            )
            ChannelInboundService._fail_binding(
                db, binding, "Agent has no active environment"
            )
            ChannelInboundService._record_ingest_failure(
                channel=channel,
                binding_thread_key=binding.thread_key,
                external_message_id=external_message_id,
                reason="no_active_environment",
            )
            # ``settle``: the failure is the reply, so it takes the notice's
            # place and stays. Releasing the id — which happens only if the
            # write landed — is what stops the next turn from rewriting a
            # message that is now the last thing this thread was told. If it did
            # not land the sender was never told it, so the id is kept on purpose
            # and the next write patches the standing notice.
            await ChannelOutboundService.set_binding_status(
                db=db,
                channel=channel,
                binding=binding,
                text=REPLY_SETUP_FAILED,
                settle=True,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "%s Ingest failed for binding %s", _LOG_PREFIX, binding.id
            )
            # Everything that could raise is resolved before the recording
            # calls below, and the thread key is read off the binding while the
            # instance is still usable — the same discipline ``_drain_parked``
            # applies in its own handlers, and for the same reason: a lazy
            # reload inside an ``except`` block replaces the exception being
            # recorded and loses the diagnosis.
            binding_thread_key = binding.thread_key
            failure = routing_trace.describe_exception(exc)
            # The failing ingest may have left the transaction poisoned; without
            # this rollback the status write below would itself raise, out of
            # the handler.
            db.rollback()
            ChannelInboundService._fail_binding(db, binding, _log_detail(exc))
            # **The operator surface for a delivery that produced nothing.**
            # Without this a soft-error ingest was invisible from every angle
            # at once: no message in the transcript, a generic notice to the
            # sender that says "setting up" rather than "your message did not
            # arrive", and nothing at all in the debug feed. The feed is where
            # an admin looks when a person says "I sent it and nothing
            # happened", and it was the one surface that could have answered.
            ChannelInboundService._record_ingest_failure(
                channel=channel,
                binding_thread_key=binding_thread_key,
                external_message_id=external_message_id,
                reason=failure or "unknown",
            )
            # ``settle``: the failure is the reply, so it takes the notice's
            # place and stays. Releasing the id — which happens only if the
            # write landed — is what stops the next turn from rewriting a
            # message that is now the last thing this thread was told. If it did
            # not land the sender was never told it, so the id is kept on purpose
            # and the next write patches the standing notice.
            await ChannelOutboundService.set_binding_status(
                db=db,
                channel=channel,
                binding=binding,
                text=REPLY_SETUP_FAILED,
                settle=True,
            )
            return False

    @staticmethod
    def _record_ingest_failure(
        *,
        channel: ServerChannel,
        binding_thread_key: str,
        external_message_id: str | None,
        reason: str,
    ) -> None:
        """Publish "this message reached an agent's session and became nothing".

        Separate from the caller only so the two failure arms cannot drift, and
        total by construction: ``ChannelDebugBuffer.record`` never raises, and
        every argument is a plain local resolved before the call (Python
        evaluates arguments before entering a function, so the buffer's own
        guard covers none of them).

        ``reason`` is the de-tainted failure class from
        ``routing_trace.describe_exception``, never ``str(exc)``: the debug
        feed is a superuser read surface and this module draws that line
        everywhere else too. The untainted text is in the log line and in
        ``binding.last_error``, both operator surfaces.
        """
        debug_channel_id = _debug_channel_key(channel)
        if debug_channel_id is None:
            return
        ChannelDebugBuffer.record(
            channel_id=debug_channel_id,
            direction="inbound",
            kind=DEBUG_REJECTED,
            summary=(
                "The message was accepted but no session message was created "
                "— the binding was failed so the next message re-routes"
            ),
            thread_key=binding_thread_key,
            detail={
                "stage": "ingest_failed",
                "failure": reason,
                "external_message_id": external_message_id or "unknown",
            },
        )

    @staticmethod
    async def _install_and_park(
        *,
        db: DBSession,
        channel: ServerChannel,
        user: User,
        bundle: AgentBundle,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        policy: ResolvedChannelPolicy,
        status_message_id: str | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> ChannelThreadBinding | None:
        """Install the matched bundle and park the message until the env is up.

        ``file_ids`` are parked with the text (§3.2). Ids only, never bytes:
        the rows they name already exist, are ``temporary``, and are reclaimed
        by ``GarbageCollectionService.cleanup_abandoned_temp_files`` after 24h
        if this binding never drains — comfortably longer than the
        pending-install age cap the scheduler enforces.

        ``policy`` is carried only to hand on to :meth:`_handle_lost_race`,
        which needs the sender's reading for *this* message and has no safe
        default to fall back on.

        ``status_message_id`` is the thread's open status notice, handed over
        so the install announces itself by rewriting it instead of posting
        underneath it. ``None`` is a transport that has no such notice, and the
        announcement falls back to an ordinary message.

        **Returns the binding that now owns that notice**, or ``None`` when
        nothing does — which is the lost-race branch, where the refusal has
        already been settled into it. The caller needs the distinction because
        it is the caller's failure handler that has to write the last word: an
        adopted notice must be settled *through the binding*, so the id is
        released and the pending-flush loop cannot come back minutes later and
        patch "ready" over a failure message.
        """
        from app.services.bundles.install_service import InstallService

        agent = await InstallService.install_bundle(db, user, bundle)

        await ChannelInboundService._audit(
            db=db,
            user_id=user.id,
            event_type=security_event_constants.SERVER_CHANNEL_AUTO_INSTALL,
            severity="low",
            details={
                "server_channel_id": str(channel.id),
                "bundle_uuid": str(bundle.id),
                "bundle_id": bundle.bundle_id,
                "agent_id": str(agent.id),
            },
        )

        binding, created = ChannelInboundService._upsert_binding(
            db=db,
            channel=channel,
            user=user,
            agent=agent,
            thread_key=thread_key,
            status=CHANNEL_BINDING_PENDING_INSTALL,
            external_message_id=external_message_id,
        )
        if not created:
            # Raced. Defer to the winner — parking onto an already-`active`
            # binding would strand the message, since the flush loop only ever
            # selects `pending_install`. Our notice goes with our result and is
            # handed down to be settled with the refusal; the winner's binding
            # owns the thread's notice from here.
            await ChannelInboundService._handle_lost_race(
                db=db,
                channel=channel,
                binding=binding,
                sender_user_id=user.id,
                thread_key=thread_key,
                text=text,
                file_ids=file_ids,
                external_message_id=external_message_id,
                external_user_id=external_user_id,
                policy=policy,
                status_message_id=status_message_id,
                redelivered_file_ids=redelivered_file_ids,
            )
            # ``_handle_lost_race`` disposed of the notice — settled with a
            # refusal, or deleted when it delivered through the winner. Nothing
            # owns it now, and nothing should rewrite it again.
            return None

        ChannelInboundService._append_parked(
            db, binding, text, external_message_id, external_user_id, file_ids
        )
        db.add(binding)
        db.commit()

        # Announced only now: before the binding is confirmed ours, a lost race
        # would have told the sender "setting up X for you" and then declined
        # them in the next breath.
        #
        # The notice is adopted first so the announcement rewrites it in place
        # — and so the flush loop, which runs minutes later in a different task
        # with nothing but this row to go on, can find it and carry it through
        # "ready" to deletion.
        #
        # Between the adopt and the ``return`` the row owns the notice and the
        # caller does not know it yet: ``bound`` is only assigned from the
        # return value. That window is safe ONLY because both statements below
        # are total — ``adopt_status_notice`` by its own contract, and
        # ``set_binding_status`` because ``set_status`` guards its adapter
        # lookup against every exception rather than just ``ChannelError``.
        # Narrow that guard back and this window reopens: the caller's handler
        # would settle a local it no longer owns and leave the row's id for the
        # flush loop to patch "ready" over the failure the sender was shown.
        ChannelOutboundService.adopt_status_notice(db, binding, status_message_id)
        await ChannelOutboundService.set_binding_status(
            db=db,
            channel=channel,
            binding=binding,
            text=REPLY_INSTALLING.format(agent_name=bundle.display_name),
        )
        return binding

    # ==================================================================
    # Pending flush (scheduler entry point)
    # ==================================================================

    @staticmethod
    async def flush_pending_bindings(db: DBSession) -> int:
        """Deliver parked messages for bindings whose environment is now ready.

        Called by the scheduler, and directly by tests. Each binding is handled
        in its own try/except so one bad row never starves the rest.

        Returns the number of bindings advanced to ``active``.
        """
        bindings = db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.status == CHANNEL_BINDING_PENDING_INSTALL
            )
        ).all()

        advanced = 0
        for binding in bindings:
            try:
                if await ChannelInboundService._flush_one(db, binding):
                    advanced += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Failed to flush binding %s", _LOG_PREFIX, binding.id
                )
                # Every binding in this tick shares one session. A failure that
                # left the transaction in a failed state would make each
                # remaining binding raise PendingRollbackError on its first
                # query — one bad row starving the rest, which is precisely
                # what the per-binding try/except exists to prevent. Reset the
                # session so the next iteration starts clean.
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001 — never mask the real error
                    logger.exception(
                        "%s Could not roll back after binding %s",
                        _LOG_PREFIX,
                        binding.id,
                    )
        return advanced

    @staticmethod
    async def _flush_one(db: DBSession, binding: ChannelThreadBinding) -> bool:
        channel = db.get(ServerChannel, binding.server_channel_id)
        agent = db.get(Agent, binding.agent_id)
        user = db.get(User, binding.user_id)
        if channel is None or agent is None or user is None:
            return False

        # The scheduler reaches here with no live resolution to carry — this
        # tick is not downstream of any webhook — so it legitimately makes its
        # own. **One reading per message still holds**: this fresh value is the
        # only reading for every parked message drained below, exactly as the
        # webhook's carried value is the only reading on ``_continue_thread``.
        # Neither path ever holds two, so there is nothing for a channel edited
        # mid-flight to make disagree; the edit simply lands on the next
        # message, or the next tick.
        policy = ChannelPolicyService.resolve(db, channel, user.id)

        env = ChannelInboundService._agent_environment(db, agent)
        status = env.status if env else None
        failure: str | None = None

        if env is None or agent.active_environment_id is None:
            # No environment to wait for. This does not self-heal, so retrying
            # forever would wedge the thread silently.
            failure = "Agent has no active environment"
        elif status in _ENV_FAILED:
            failure = f"Environment build failed (status={status})"
        # NOTE: `critical_state` is deliberately NOT a failure here, diverging
        # from plan §8 ("env error/critical -> failed"). That wording predates
        # the current semantics: `critical_state` coexists with
        # status="running" — a degraded-but-up container whose sessions, chat
        # and terminals all keep working. It would have answered the user, so
        # telling an external person "setup failed" and burning the binding
        # would be wrong. Only `status == "error"` is terminal.
        elif status not in _ENV_READY:
            # Still building/starting/suspended. Bounded: a binding that never
            # becomes ready fails rather than retrying indefinitely.
            if ChannelInboundService._binding_age_seconds(binding) > _PENDING_MAX_AGE_SECONDS:
                failure = (
                    f"Environment did not become ready in time (status={status})"
                )
            else:
                return False

        if failure is not None:
            ChannelInboundService._fail_binding(db, binding, failure)
            # ``settle``: the failure is the reply, so it takes the notice's
            # place and stays. Releasing the id — which happens only if the
            # write landed — is what stops the next turn from rewriting a
            # message that is now the last thing this thread was told. If it did
            # not land the sender was never told it, so the id is kept on purpose
            # and the next write patches the standing notice.
            await ChannelOutboundService.set_binding_status(
                db=db,
                channel=channel,
                binding=binding,
                text=REPLY_SETUP_FAILED,
                settle=True,
            )
            return False

        # Environment is ready. Flip to `active` and announce it BEFORE
        # draining: the flush query only selects `pending_install`, so this is
        # what stops a mid-drain failure from re-announcing "ready" and
        # re-delivering messages on every 45-second tick.
        if binding.pending_messages:
            # Advances the same notice the install opened, minutes ago and in
            # another task — the id is on the binding, which is why it was
            # adopted there rather than kept in ``_route_new_thread``'s frame.
            # Not settled: the drain below produces a real reply, and
            # ``handle_stream_completed`` deletes the notice when it lands.
            await ChannelOutboundService.set_binding_status(
                db=db, channel=channel, binding=binding, text=REPLY_READY
            )
        binding.status = CHANNEL_BINDING_ACTIVE
        binding.last_error = None
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()

        await ChannelInboundService._drain_parked(
            db=db,
            channel=channel,
            binding=binding,
            agent=agent,
            user=user,
            policy=policy,
        )
        return True

    @staticmethod
    def _binding_age_seconds(binding: ChannelThreadBinding) -> float:
        created = binding.created_at
        if created is None:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created).total_seconds()

    @staticmethod
    async def _drain_parked(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        policy: ResolvedChannelPolicy,
    ) -> None:
        """Deliver parked messages in arrival order, exactly once each.

        **Entries are read defensively, not by index.** ``file_ids`` is a key
        this feature added to a JSON column that already had rows in it, so a
        binding parked before the deploy drains after it with no such key —
        hence ``entry.get("file_ids") or []`` and never ``entry["file_ids"]``,
        which would raise a ``KeyError`` and lose a real message. The ids are
        parsed with the same posture; see ``_parse_parked_file_ids``. They are
        then filtered against the rows that still exist
        (``_surviving_parked_file_ids``), because a binding stuck past the 24h
        temp-file GC would otherwise lose its **text** as well as its files —
        the attach path refuses the whole message when a single id no longer
        resolves.

        Each entry is removed only AFTER its ingest succeeds, and the removal
        is committed immediately — so a crash mid-drain re-delivers at most the
        one in flight, and a failure leaves the rest parked rather than losing
        them. Stops on the first failure: the messages are a conversation, and
        replaying later ones out of order past a gap would be worse than
        waiting.

        **Two kinds of failure, two dispositions.** Keeping the messages parked
        is the right answer only for a failure a later attempt might not hit.
        ``_ingest`` also raises ``ChannelDecline`` for declines that
        will recur identically on every retry — the sender's
        ``allow_identity_routing`` consent being off (re-read per message from
        ``policy``), ``assert_access`` refusing a revoked or invalid identity
        grant, and the ``user.id != binding.user_id`` invariant guard. Retrying
        one of those forever would wedge the thread until the parked-message
        cap is hit, sending the person a setup-failed notice each time and
        never arming the ``failed`` self-heal that is the actual recovery. So a
        deterministic decline fails the binding instead; see the handler.

        The classifier gates on ``ChannelDecline`` and not on
        ``PermissionError``, deliberately: ``PermissionError`` is an
        ``OSError``, so the first real I/O under ``_ingest`` would start
        raising the "will never succeed" marker for failures that are merely
        transient — and the disposition for those is the opposite one. Every
        decline path below this method raises the narrower type (see its own
        docstring); the broad ``except`` further down still catches anything
        that does not.

        **This is pre-emptive, not a live bug fix.** Today identity-routed
        messages and parked messages are disjoint: parking requires binding
        status ``pending_install``, which only routing Pass 2 (auto-install)
        creates, and Pass 2 is barred on the identity branch — so no identity
        message can currently reach this drain. The invariant guard is likewise
        unreachable from here (every caller loads ``db.get(User,
        binding.user_id)``). What this removes is a wedge that any later phase
        making an identity message parkable would otherwise discover in
        production, where the symptom — a thread answering nothing, forever,
        with no failed binding to explain it — is expensive to diagnose.
        """
        # Read the binding's identity ONCE, here, outside every exception
        # handler below — the same fix ``_route_and_bind`` uses and the one
        # ``_debug_channel_key``'s docstring calls the better of the two.
        # ``_flush_one`` commits immediately before calling this, so these
        # instance attributes are expired and reading one is a lazy reload that
        # raises ``ObjectDeletedError`` if the row went away. Out here that
        # propagates to ``flush_pending_bindings``' per-binding guard, which is
        # correct; inside the handler it would *replace* the exception being
        # recorded, which is the failure mode ``ChannelOutboundService._deliver``
        # documents from a real incident.
        binding_id = binding.id
        binding_thread_key = binding.thread_key

        while True:
            parked = list(binding.pending_messages or [])
            if not parked:
                return
            entry = parked[0] or {}
            text = entry.get("text") or ""
            # ``.get`` and never ``entry["file_ids"]``: a binding parked
            # BEFORE this feature deployed is drained after it, and a KeyError
            # here would lose a real message the sender is still waiting on.
            file_ids = _parse_parked_file_ids(entry.get("file_ids"), binding_id)
            # Filtered against what still exists. A binding stuck past the 24h
            # temp-file GC drains with ids naming rows that are gone, and
            # ``prepare_user_message_with_files`` refuses the *whole* message
            # when any one id does not resolve — turning a partial loss into a
            # total one, text included. See ``_surviving_parked_file_ids``.
            file_ids, missing_files = _surviving_parked_file_ids(
                db, file_ids, binding_id
            )
            text = _compose_drained_text(text, missing_files)

            # ``or file_ids``: an attachment-only message parks with EMPTY
            # text (the composed note is empty when nothing was skipped), so
            # gating on text alone would drop the entry and silently lose the
            # files it was the whole point of. When files expired, the note
            # above is itself the text, which keeps the message deliverable
            # instead of vanishing.
            if text or file_ids:
                try:
                    await ChannelInboundService._ingest(
                        db=db,
                        channel=channel,
                        binding=binding,
                        agent=agent,
                        user=user,
                        text=text,
                        file_ids=file_ids,
                        sender_external_id=entry.get("external_user_id"),
                        policy=policy,
                    )
                except ChannelDecline as exc:
                    # ---- Deterministic decline ----
                    #
                    # Every argument expression that could raise is resolved
                    # first, through the total helpers, before anything records:
                    # Python evaluates a call's arguments before entering it, so
                    # ``ChannelDebugBuffer.record``'s never-raises guard covers
                    # none of them. ``binding_id`` / ``binding_thread_key`` are
                    # already plain locals from the top of the method.
                    debug_channel_id = _debug_channel_key(channel)
                    detail = _log_detail(exc)
                    failure = routing_trace.describe_exception(exc)
                    dropped = len(parked)

                    # The failed ingest may have poisoned the transaction; the
                    # writes below would otherwise raise out of this handler.
                    db.rollback()

                    # **Disposition: drop the parked queue, deliberately and
                    # visibly.** The binding is about to be failed, and a failed
                    # binding self-heals by being *deleted* on the next inbound
                    # message so routing runs again from scratch. Nothing
                    # carries a pending_messages queue across that delete, and
                    # leaving the entries on a doomed row would be a silent
                    # loss dressed up as a queue. Nor could they be replayed
                    # afterwards: the re-route may pick a different agent (for a
                    # withdrawn identity consent it deliberately will — the
                    # sender falls back to their own agents), so the parked text
                    # would be delivered somewhere it was never addressed. The
                    # count is logged, persisted in ``last_error`` and published
                    # to the debug feed, so the drop is observable rather than
                    # silent — which is this feature's standing rule.
                    binding.pending_messages = []
                    flag_modified(binding, "pending_messages")
                    ChannelInboundService._fail_binding(
                        db,
                        binding,
                        "Deterministic decline while draining parked messages "
                        f"({failure}) — binding failed to arm the self-heal; "
                        f"dropped {dropped} parked message(s). Cause: {detail}",
                    )
                    logger.warning(
                        "%s Deterministic decline draining parked messages: "
                        "binding=%s channel=%s — failing the binding so the "
                        "next message re-routes, and dropping %d parked "
                        "message(s). Cause: %s",
                        _LOG_PREFIX,
                        binding_id,
                        debug_channel_id or "unknown",
                        dropped,
                        # ``_log_detail(exc)``, not ``exc``: see that helper.
                        # ``logging``'s lazy interpolation is not covered by
                        # anything this handler controls, and pytest's capture
                        # handler re-raises what production would swallow.
                        detail,
                    )
                    if debug_channel_id is not None:
                        ChannelDebugBuffer.record(
                            channel_id=debug_channel_id,
                            direction="inbound",
                            kind=DEBUG_REJECTED,
                            summary=(
                                "Parked message declined deterministically "
                                "while draining — the binding was failed so "
                                "the next message re-routes from scratch, and "
                                f"{dropped} parked message(s) were dropped"
                            ),
                            thread_key=binding_thread_key,
                            detail={
                                "stage": "parked_drain",
                                "failure_class": "deterministic",
                                # De-tainted (type + status), not
                                # ``str(exc)``: the buffer is a superuser read
                                # surface and this file already draws that line
                                # in ``_reply``. The untainted text is in the
                                # log line above, an operator surface.
                                "failure": failure or "unknown",
                                "dropped_parked_messages": str(dropped),
                                "binding_id": str(binding_id),
                            },
                        )
                    # The SAME generic notice every other refusal gets. A reply
                    # this sender could tell apart from a transient setup
                    # failure would be an oracle for the channel's
                    # configuration — see the comments around the
                    # ``ChannelDecline`` raise in ``_ingest``. ``settle``
                    # releases the notice id only if this write landed; a settle
                    # that failed keeps it, so the next write patches the
                    # standing notice rather than posting beneath it.
                    await ChannelOutboundService.set_binding_status(
                        db=db,
                        channel=channel,
                        binding=binding,
                        text=REPLY_SETUP_FAILED,
                        settle=True,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    # ---- Transient failure ----
                    #
                    # Unchanged: record the diagnosis, keep the messages parked
                    # for a later attempt, and tell the person now.
                    logger.exception(
                        "%s Failed to deliver parked message for binding %s",
                        _LOG_PREFIX,
                        binding_id,
                    )
                    db.rollback()
                    # ``_log_detail(exc)``, not ``str(exc)``: this is inside an
                    # ``except`` block, exactly the unguarded-``__str__`` shape
                    # ``_log_detail`` exists for — a raise from ``exc.__str__``
                    # here would replace the exception being recorded and lose
                    # the diagnosis entirely. ``_log_detail`` cannot raise and
                    # is identical to ``str(exc)`` for every real exception, so
                    # this is hardening, not a behaviour change. Already
                    # truncates to 2000 internally; no need to slice again.
                    binding.last_error = _log_detail(exc)
                    db.add(binding)
                    db.commit()
                    # Never strand silently: the remaining messages stay parked
                    # and will be retried on the next inbound message, but the
                    # person is owed an answer now. ``settle`` releases the
                    # notice id only if this write landed; a settle that failed
                    # keeps it, so the next write patches the standing notice
                    # rather than posting beneath it.
                    await ChannelOutboundService.set_binding_status(
                        db=db,
                        channel=channel,
                        binding=binding,
                        text=REPLY_SETUP_FAILED,
                        settle=True,
                    )
                    return

            binding.pending_messages = parked[1:]
            flag_modified(binding, "pending_messages")
            binding.last_error = None  # healed; don't leave a stale diagnosis
            binding.updated_at = datetime.now(UTC)
            db.add(binding)
            db.commit()

    # ==================================================================
    # Ingestion
    # ==================================================================

    @staticmethod
    async def _ingest(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        text: str,
        policy: ResolvedChannelPolicy,
        sender_external_id: str | None = None,
        identity_grant: IdentityGrant | None = None,
        file_ids: list[uuid.UUID] | None = None,
        redelivered_file_ids: set[uuid.UUID] | None = None,
    ) -> None:
        """Hand the message to the canonical ingestion service.

        The session is resumed by id when the binding already has one, and
        created otherwise. ``session_id`` is NULL both before the first message
        and after the session was deleted (the SET NULL cascade), so one branch
        covers first contact and recovery alike.

        ``sender_external_id`` is the platform-native id captured at the webhook
        (``google_chat:users/123``). It is metadata only — the authoritative
        identity is ``user.id`` — so when it is unavailable (a resume, or a
        parked message from before this field was recorded) the constructor
        falls back to the platform user id.

        **``user`` is always the binding's user — and this method now enforces
        that rather than merely asserting it.** Every caller already satisfies
        it (``_route_new_thread`` passes the sender it just bound;
        ``_continue_thread``, ``_handle_lost_race`` and the drain paths all
        load ``db.get(User, binding.user_id)``), but "every caller today" is
        not something a downstream guard can rest on, and Phase 4 adds a second
        poller into these helpers. The check at the top of the body makes
        ``sender.platform_user_id == binding.user_id`` true by construction.

        What that buys, precisely: it protects the **unchanged, non-identity**
        arm of ``ChannelIngestionService._verify_resume_sender``
        (``existing.user_id == sender.platform_user_id``). The binding pins
        ``session_id`` and the sender is the binding's user, so an ordinary
        channel session can only ever be resumed by the person the thread
        belongs to. It is **not** what makes that method's identity exception
        safe: that arm compares ``existing.identity_caller_id`` against the
        sender on the session row itself, and refuses a deliberately
        mismatched ``(binding, user)`` pair without any help from here. And
        neither arm is the authorization — ``assert_access`` runs first, on
        every message, and re-reads the whole grant.

        **Identity: the session may be owned by someone other than the
        sender.** Two sources, never mixed:

        - *First message of a routed thread* — ``identity_grant`` is the claim
          Stage 2 produced (plan §2.3). The identity ids are stamped onto the
          new session, and ``session.user_id`` becomes the identity owner.
        - *Every later message* — the claim is rebuilt from the session row
          itself, which carries all three ids. Rebuilding rather than caching
          is the point: ``assert_access`` re-reads and re-verifies all six
          conditions on **every** message, so an owner who revokes mid-thread
          is honored on the next turn rather than at the next thread. That
          extends Phase 2's decided semantic — a revoked sender is declined on
          an existing thread exactly as on a new one — and, like it, the
          decline is detail-free: the ``ChannelDecline`` is caught by
          ``_ingest_or_fail``, which sends the same generic reply every other
          failure gets.

        **The sender's own consent is re-read on every message too**, from
        ``policy`` — which is this message's single reading of their channel
        settings, carried in from the webhook or made fresh by the scheduler
        drain, never both. Turning ``allow_identity_routing`` off (or resetting
        the channel, which drops the row and returns the column to its ``false``
        default) therefore stops the *existing* identity thread on its next
        message, not only the next new one. A consent switch that could not be
        withdrawn on the very conversation it authorized would be no consent at
        all; and this is the same semantic already decided for a revoked grant,
        applied to the other side of the same permission. An ordinary channel
        thread has no grant, so it never consults the flag.

        ``integration_type`` stays ``channel_<type>`` on the identity path.
        ``ChannelOutboundService._resolve_channel_session`` gates on that
        prefix, so a session stamped ``identity_mcp`` here would route
        correctly, run correctly, and never deliver a reply.

        **``file_ids`` and the one non-default ``uploader_user_id`` on the
        platform.** The sender's attachments are owned by the *sender*, while
        an identity-routed session is owned by someone else — so the ownership
        check inside ``MessageService.prepare_user_message_with_files``, which
        compares each file against the *session* owner, would refuse them.
        ``uploader_user_id`` tells that check who actually uploaded the bytes.

        This is the only call site in the codebase that passes it as anything
        but ``None``, and this method is where that is safe to do: the guard at
        the top of the body has already *enforced* ``user.id ==
        binding.user_id``, so the value is not a caller's claim about identity
        but an invariant this frame just checked. The authorisation — may this
        person write into this session at all — was settled upstream by
        ``assert_access``, which re-reads the whole grant on every message.
        ``uploader_user_id`` answers *who uploaded these bytes*, never *may
        they*. A second caller wanting it is a design conversation, not a
        parameter.
        """
        from app.core.db import create_session
        from app.models import Session as ChatSession

        # The invariant this method's contract is written on, enforced at the
        # only place all of its callers meet. A mismatched pair here would put
        # one person's text into another's thread; refusing it costs one
        # comparison and cannot be forgotten by a future caller the way a
        # docstring can.
        if user.id != binding.user_id:
            raise ChannelDecline(
                "channel ingest sender does not own the binding: "
                f"user.id={user.id}, binding.user_id={binding.user_id}"
            )

        sender = SessionSender.from_channel(
            channel_type=channel.channel_type,
            external_user_id=sender_external_id or str(user.id),
            platform_user_id=user.id,
            display_name=None,
        )

        # Resume only if the session still exists. The FK nulls `session_id` on
        # delete, but a delete racing this read would leave a stale pointer that
        # `resolve_or_create_session` would reject with "Session not found".
        existing: ChatSession | None = None
        thread_key: uuid.UUID | None = binding.session_id
        if thread_key is not None:
            existing = db.get(ChatSession, thread_key)
            if existing is None:
                thread_key = None

        if existing is not None:
            # Resume: the row is the only admissible source. A grant handed in
            # by a caller would be describing a different message.
            grant = ChannelInboundService._resume_identity_grant(existing)
        else:
            # Create. On the recovery branch — the binding's session was
            # deleted, so `thread_key` was just cleared — a continue/drain call
            # arrives with no grant, and if the bound agent is a foreign one
            # `assert_access` refuses. That is deliberate rather than repaired
            # here: nothing in this call is evidence the identity is still
            # shared, and re-deriving one from the binding would be inventing
            # an authorization the routing layer never issued. The refusal
            # fails the binding, which self-heals — the next message deletes it
            # and re-routes, producing a fresh, freshly-verified grant.
            grant = identity_grant

        # Consent, re-read per message from THIS message's single reading (see
        # the docstring). Short-circuited on `grant is None`, so an ordinary
        # channel thread never reaches the flag and is unaffected.
        #
        # Raised detail-free on purpose: `_ingest_or_fail` turns it
        # into the one generic reply every other refusal gets, so this decline
        # is indistinguishable from a revoked grant, a vanished binding, or a
        # failed environment. A reply that named it would be an oracle telling
        # an external sender which gate closed.
        if grant is not None and not policy.allow_identity_routing:
            raise ChannelDecline(
                "identity routing is switched off for this sender on this "
                "channel"
            )

        extra_session_kwargs: dict[str, Any] | None = None
        if thread_key is None:
            session_metadata_extra: dict[str, Any] = {
                "server_channel_id": str(channel.id),
                "thread_key": binding.thread_key,
                "sender_external_id": sender.external_id,
            }
            extra_session_kwargs = {
                "session_metadata_extra": session_metadata_extra
            }
            if grant is not None:
                # Attribution for the person who did not start this
                # conversation. The session opens in the identity OWNER's
                # space, so it appears in their list containing a stranger's
                # message; without a name the only identification is a raw uuid
                # in a column nothing renders. Same key App MCP's identity path
                # stamps (`external_a2a_context_handler`), so the session view
                # needs one branch for both, not two.
                #
                # `identity_owner_name` — the other half of that pair — is
                # deliberately NOT stamped: on this path the owner is the
                # session's own user, so it would be telling the reader their
                # own name.
                session_metadata_extra["identity_caller_name"] = (
                    (user.full_name or "").strip() or user.email
                )
                extra_session_kwargs.update(
                    {
                        # Consumed by `_select_session_owner_id`: the session is
                        # the identity owner's, not the sender's.
                        "identity_owner_id": grant.owner_id,
                        # Post-create stamps; all three are already in
                        # `ChannelIngestionService._STAMPABLE_COLUMNS`, and all
                        # three are what the resume path above reads back.
                        "identity_caller_id": user.id,
                        "identity_binding_id": grant.binding_id,
                        "identity_binding_assignment_id": grant.assignment_id,
                    }
                )

        result = await ChannelIngestionService.ingest_inbound_message(
            db=db,
            agent=agent,
            sender=sender,
            thread_key=thread_key,
            content=text,
            file_ids=file_ids or None,
            # ``binding.user_id`` and not ``user.id``, though the guard at the
            # top of this method has just made them equal: the parameter's
            # contract is about the *binding's* owner, and reading it off the
            # binding is what keeps that legible after the guard scrolls away.
            #
            # ``if file_ids`` and not unconditionally. The value is only ever
            # *read* in the ``has_files`` branch, so passing it on a text-only
            # message is inert today — but it is the one deliberate widening of
            # an ownership check on the platform (§5.5), and a widening that
            # travels on every channel message is a wider blast radius than its
            # own docstring claims. It rides with the files or not at all.
            uploader_user_id=binding.user_id if file_ids else None,
            # **The narrow half of the redelivery fix.** ``materialize``
            # deliberately REUSES the rows an earlier delivery of this same
            # external message already created — that is what stops a retry
            # re-fetching from Google and charging the sender's quota a second
            # time — but the first delivery flipped those rows from
            # ``temporary`` to ``attached``, and
            # ``prepare_user_message_with_files`` refuses an attached file.
            # Both rules are right; the collision is resolved by naming the
            # exact ids that are a redelivery of a message that already owns
            # them. Everything not named is refused exactly as before, so the
            # protection against a file drifting into an *unrelated* message
            # survives intact. Rides with the files or not at all, for the same
            # reason ``uploader_user_id`` does.
            redelivered_file_ids=redelivered_file_ids if file_ids else None,
            integration_type=f"channel_{channel.channel_type}",
            access_policy=ChannelAccessPolicy(
                # The owner the three-way invariant is checked against. On the
                # identity path that is the identity owner (which is also
                # `agent.owner_id`), not the sender — the sender is the
                # `identity_caller_id`, and the grant beside it is what makes
                # the mismatch legitimate.
                expected_owner_id=grant.owner_id if grant is not None else user.id,
                identity_grant=grant,
            ),
            get_fresh_db_session=create_session,
            extra_session_kwargs=extra_session_kwargs,
        )

        # **A soft error is a failure, and this is where it becomes one.**
        # ``ingest_inbound_message`` reports a whole family of failures — no
        # active environment, activation failed, session vanished, file
        # preparation refused — as ``action="error"`` with no exception, and
        # every caller above this frame decides "delivered or not" by asking
        # whether something raised. Left as a quiet return value it is
        # indistinguishable from a delivered message: the binding gets stamped,
        # or the parked entry gets popped, and the sender's message is gone for
        # good (the next redelivery dedups against the stamp).
        #
        # Raised *before* the binding's ``session_id`` is updated below. On the
        # failure path the session may well have been created — the error came
        # later — but writing it here would pair a live pointer with a binding
        # about to be marked failed, and the self-heal deletes that binding on
        # the next message anyway.
        #
        # ``result.message`` is the friendly text ``send_session_message``
        # returns beside the error; it is for the operator log and
        # ``binding.last_error`` only. The sender is told the same generic
        # setup-failed notice every other ingest failure produces — see
        # ``_ingest_or_fail``.
        if result.action == "error":
            raise ChannelIngestProducedNoMessage(
                f"ingest produced no message: {result.message or 'unknown error'}"
            )

        if binding.session_id != result.session.id:
            binding.session_id = result.session.id
            binding.updated_at = datetime.now(UTC)
            db.add(binding)
            db.commit()

        ChannelInboundService._record_routing_outcome(
            db=db,
            channel=channel,
            thread_key=binding.thread_key,
            agent_id=agent.id,
            session_id=result.session.id,
        )

    @staticmethod
    def _record_routing_outcome(
        *,
        db: DBSession,
        channel: ServerChannel,
        thread_key: str,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> None:
        """Tell the transport which agent and session this thread ended up on.

        A no-op for every transport that keeps no durable record of an arrival,
        which is all of them but email. Email persists each polled mail
        *before* classification — the messages worth recording most are the
        ones a denial stops before an agent exists — so the row is written
        without an agent and stamped here, at the one point in the pipeline
        where both answers are known and every successful ingest passes
        through.

        Placed after the binding commit rather than before it: this is
        bookkeeping riding on a delivery that has already happened, not a step
        the delivery depends on.

        **Swallowed on failure, deliberately.** An audit stamp that could fail
        an ingest would turn a message the agent has already received into a
        failed binding, a setup-failed notice to the sender, and a thread that
        self-heals by re-routing text that was delivered once already. The
        stamp is worth having; it is not worth that. The rollback keeps a
        poisoned transaction from surfacing as an unrelated error in the
        caller.
        """
        try:
            adapter = get_adapter(channel.channel_type)
            adapter.record_routing_outcome(
                db,
                channel,
                thread_key=thread_key,
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 — see the docstring
            logger.warning(
                "%s Could not record the routing outcome for thread %s on "
                "channel %s — the message was delivered regardless",
                _LOG_PREFIX,
                thread_key,
                _debug_channel_key(channel) or "unknown",
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 — nothing left to salvage
                pass

    @staticmethod
    def _resume_identity_grant(session: "ChatSession") -> IdentityGrant | None:
        """Rebuild the identity claim for a session being resumed.

        ``None`` for an ordinary channel session — every column below is NULL
        there, and the caller then uses the sender's own id as the expected
        owner exactly as before.

        The three ids were stamped when the session was created and are re-read
        rather than remembered, which is the whole point: this produces a
        *claim*, and ``ChannelIngestionService.assert_access`` re-verifies all
        six conditions behind it against the database on this message. A row
        whose ids no longer describe a live, enabled, correctly-linked binding
        is refused, so revocation takes effect on the very next message.

        ``owner_id`` comes from ``session.user_id`` because that is what
        identity ownership *means* here (the session lives in the owner's
        space). It is not trusted: condition 6 pins
        ``binding.owner_id == agent.owner_id == owner_id``, so a row whose
        ``user_id`` disagrees with its own binding is rejected rather than
        honored.

        All three columns are required. A partially-stamped row cannot be
        completed by guessing — the missing piece is precisely the linkage the
        six conditions exist to check — so it degrades to "no grant" and is
        refused by the ordinary invariant.
        """
        if (
            session.identity_caller_id is None
            or session.identity_binding_id is None
            or session.identity_binding_assignment_id is None
        ):
            return None
        return IdentityGrant(
            owner_id=session.user_id,
            binding_id=session.identity_binding_id,
            assignment_id=session.identity_binding_assignment_id,
        )

    # ==================================================================
    # Binding helpers
    # ==================================================================

    @staticmethod
    def _get_binding(
        db: DBSession, channel_id: uuid.UUID, thread_key: str
    ) -> ChannelThreadBinding | None:
        return db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.server_channel_id == channel_id,
                ChannelThreadBinding.thread_key == thread_key,
            )
        ).first()

    @staticmethod
    def _upsert_binding(
        *,
        db: DBSession,
        channel: ServerChannel,
        user: User,
        agent: Agent,
        thread_key: str,
        status: str,
        external_message_id: str | None,
    ) -> tuple[ChannelThreadBinding, bool]:
        """Create the binding, tolerating a concurrent first-message race.

        Returns ``(binding, created)``. Two messages arriving simultaneously in
        one brand-new thread both reach routing; the unique constraint on
        (server_channel_id, thread_key) lets exactly one insert win, and the
        loser catches IntegrityError and re-reads the winner's row (the
        ``ServerConfigService.get_or_create`` idiom).

        ``created`` matters: the loser must defer to the winner's agent rather
        than proceeding with its own routing result, or the binding ends up
        naming one agent while its session belongs to another.
        """
        binding = ChannelThreadBinding(
            server_channel_id=channel.id,
            thread_key=thread_key,
            user_id=user.id,
            agent_id=agent.id,
            status=status,
            last_external_message_id=external_message_id,
        )
        db.add(binding)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = ChannelInboundService._get_binding(db, channel.id, thread_key)
            if existing is None:
                raise
            logger.info(
                "%s Lost binding race for thread %s — using existing binding %s",
                _LOG_PREFIX,
                thread_key,
                existing.id,
            )
            return existing, False
        db.refresh(binding)
        return binding, True

    @staticmethod
    def _park_message(
        db: DBSession,
        binding: ChannelThreadBinding,
        inbound: ChannelInboundMessage,
        *,
        text: str,
        file_ids: list[uuid.UUID] | None = None,
    ) -> bool:
        """Park an inbound message. Returns False when the cap refused it.

        ``text`` is the **composed** text — ``inbound.text`` plus any note
        about attachments that could not be accepted — and not
        ``inbound.text``, which is what this used to read off the message
        itself. A message that waits out an install must arrive with the same
        content it would have had if it had gone straight through.

        When the cap refuses the message, its already-materialised uploads are
        simply left ``temporary`` and reclaimed by the file GC; the refusal
        reply is unchanged.
        """
        accepted = ChannelInboundService._append_parked(
            db,
            binding,
            text,
            inbound.external_message_id,
            inbound.external_user_id,
            file_ids,
        )
        # Only claim delivery for a message we actually kept. Stamping a
        # message the cap refused would mark it deduped, so a redelivery would
        # be dropped too and the user would lose it silently.
        if accepted and inbound.external_message_id:
            binding.last_external_message_id = inbound.external_message_id
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()
        return accepted

    @staticmethod
    def _append_parked(
        db: DBSession,
        binding: ChannelThreadBinding,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None = None,
        file_ids: list[uuid.UUID] | None = None,
    ) -> bool:
        """Append to the parked queue. Returns False when the cap refused it.

        Reassigns the list rather than mutating in place, and flags the
        attribute: a plain ``.append()`` on a JSON column is not dirty-tracked
        and the commit would silently drop the message.

        ``file_ids`` are written as **strings**, because the queue is a JSON
        column and a ``UUID`` is not JSON-serialisable. The key is written even
        when the list is empty so every entry this deploy writes has the same
        shape; readers still use ``entry.get("file_ids") or []``, because
        entries written *before* this deploy do not have it at all (§3.2).

        Ids only, never bytes: the rows already exist and are ``temporary``, so
        a queue that never drains costs 24h of disk and then nothing.
        """
        parked = list(binding.pending_messages or [])
        if len(parked) >= _MAX_PARKED_MESSAGES:
            # Refuse the newest rather than evicting the oldest: the first
            # parked message is the one routing chose the agent from, and
            # dropping it would leave the session opening on a follow-up with
            # no context.
            logger.warning(
                "%s Binding %s is at the parked-message cap — dropping newest",
                _LOG_PREFIX,
                binding.id,
            )
            return False
        parked.append(
            {
                "text": text,
                "file_ids": [str(file_id) for file_id in file_ids or []],
                "external_message_id": external_message_id,
                "external_user_id": external_user_id,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
        binding.pending_messages = parked
        flag_modified(binding, "pending_messages")
        return True

    @staticmethod
    def _fail_binding(
        db: DBSession, binding: ChannelThreadBinding, error: str
    ) -> None:
        binding.status = CHANNEL_BINDING_FAILED
        binding.last_error = (error or "")[:2000]
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()

    @staticmethod
    def _agent_environment(db: DBSession, agent: Agent) -> AgentEnvironment | None:
        if agent.active_environment_id:
            env = db.get(AgentEnvironment, agent.active_environment_id)
            if env is not None:
                return env
        return db.exec(
            select(AgentEnvironment).where(AgentEnvironment.agent_id == agent.id)
        ).first()

    # ==================================================================
    # Misc helpers
    # ==================================================================

    @staticmethod
    def _seen_recently(key: str) -> bool:
        """True if ``key`` was seen inside the TTL. Records it either way.

        Bounded and swept, because the key embeds attacker-supplied input: an
        unbounded process dict on a public endpoint is a memory-exhaustion
        vector in its own right.
        """
        import time

        now = time.monotonic()
        cutoff = now - _RECENT_MESSAGE_TTL_SECONDS
        if len(_recent_message_ids) > _RECENT_MESSAGE_MAX_KEYS:
            for stale in [k for k, t in _recent_message_ids.items() if t < cutoff]:
                _recent_message_ids.pop(stale, None)
            if len(_recent_message_ids) > _RECENT_MESSAGE_MAX_KEYS:
                _recent_message_ids.clear()

        last = _recent_message_ids.get(key)
        _recent_message_ids[key] = now
        return last is not None and last >= cutoff

    @staticmethod
    def _schedule(coro: Any, name: str) -> None:
        from app.utils import create_task_with_error_logging

        create_task_with_error_logging(coro, name)

    @staticmethod
    async def _reply(
        db: DBSession, channel: ServerChannel, thread_key: str, text: str
    ) -> None:
        """Post a standalone message into a thread that may have no binding."""
        # §11a Rule 2, at the one instrumentation point in this file that sits
        # INSIDE an ``except`` — which is why it is fixed ahead of its known
        # siblings rather than with them. Everywhere else an unguarded argument
        # expression becomes a 500 that the platform retries; here it would
        # *replace* the exception it was recording. The delivery failure this
        # panel exists to show would vanish, and the caller would be handed a
        # database error in its place. Losing the diagnosis is categorically
        # worse than failing loudly, so this one does not wait.
        #
        # Two expressions were exposed, not one: ``channel.id`` and the
        # ``f"...{exc}"`` summary. Python evaluates both *before* entering
        # ``ChannelDebugBuffer.record``, so the buffer's never-raises guard
        # covered neither. The test is not "is the recorder guarded" but "can
        # anything in the caller's argument list raise".
        #
        # ``channel.id`` is resolved once, above the ``try``, through a total
        # helper: hoisting it a few lines up inside the same handler would only
        # relocate the raise, and reading it unguarded above the ``try`` would
        # trade a swallowed diagnosis for a notice that is never even attempted.
        debug_channel_id = _debug_channel_key(channel)
        try:
            adapter = get_adapter(channel.channel_type)
            await adapter.send_message(channel, thread_key, text)
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_REPLIED,
                    summary="Pipeline notice delivered",
                    thread_key=thread_key,
                    text=text,
                )
        except Exception as exc:  # noqa: BLE001 — best effort
            # ``describe_exception`` rather than ``f"{exc}"``. It is total by
            # construction — an exception with a raising ``__str__`` comes back
            # as ``"unavailable"`` instead of detonating in this handler — and
            # it is already this file's answer to "stringify an exception
            # inside an argument list", so the fix reuses the established tool
            # rather than growing a second local one.
            #
            # It drops the exception's message body, and that is a gain here
            # rather than a cost: an adapter's HTTP error routinely echoes the
            # request it just made, and the request this method makes carries
            # the channel's service-account credentials into a buffer that is a
            # read surface. What diagnoses a delivery failure — the exception
            # type and the HTTP status — is exactly what survives. The full
            # message is not lost either: the ``logger.warning`` below still
            # carries it, where formatting is lazy and inside ``logging``'s own
            # error handling.
            failure = routing_trace.describe_exception(exc)
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_SEND_FAILED,
                    summary=f"Notice delivery failed: {failure}",
                    thread_key=thread_key,
                    text=text,
                )
            logger.warning(
                "%s Could not deliver notice to channel=%s thread=%s: %s",
                _LOG_PREFIX,
                # The same hoisted value: this argument list is evaluated
                # eagerly too, so an inline ``channel.id`` here would destroy
                # the original exception just as surely as the one above.
                debug_channel_id or "unknown",
                thread_key,
                # ``_log_detail(exc)``, not ``exc``: see that helper. The
                # interpolation ``logging`` would do later is not covered by
                # anything this handler controls.
                _log_detail(exc),
            )

    @staticmethod
    async def _audit(
        *,
        db: DBSession,
        user_id: uuid.UUID | None,
        event_type: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        """Write a SecurityEvent, never letting audit failure break the flow."""
        if user_id is None:
            # DEFERRED (known gap): rejection events are attributed to the
            # channel's creator, and `ServerChannel.created_by` is ON DELETE
            # SET NULL. Deleting the superuser who created a channel therefore
            # silently stops verification-failure and denial auditing for it
            # from then on. The event still reaches the application log.
            logger.info(
                "%s %s (no user to attribute): %s", _LOG_PREFIX, event_type, details
            )
            return
        try:
            from app.services.events.security_event_service import SecurityEventService

            await SecurityEventService.create_event(
                session=db,
                user_id=user_id,
                data=SecurityEventCreate(
                    event_type=event_type, severity=severity, details=details
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s Failed to write security event %s", _LOG_PREFIX, event_type)

    @staticmethod
    async def _audit_throttled(
        *,
        db: DBSession,
        key: str,
        user_id: uuid.UUID | None,
        event_type: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        """Rate-limited audit for attacker-triggerable events.

        A prober hitting a valid token with bad JWTs would otherwise write one
        row per request. Process-local, like every other in-memory throttle
        here — a multi-worker deployment gets one row per worker per window,
        which is still bounded.
        """
        import time

        now = time.monotonic()
        last = _recent_security_events.get(key)
        if last is not None and now - last < _SECURITY_EVENT_THROTTLE_SECONDS:
            return
        # Bounded like its neighbours: the key embeds a sender address, so an
        # attacker varying it must not be able to grow this dict without limit.
        if len(_recent_security_events) > _SECURITY_EVENT_MAX_KEYS:
            cutoff = now - _SECURITY_EVENT_THROTTLE_SECONDS
            for stale in [k for k, t in _recent_security_events.items() if t < cutoff]:
                _recent_security_events.pop(stale, None)
            if len(_recent_security_events) > _SECURITY_EVENT_MAX_KEYS:
                _recent_security_events.clear()
        _recent_security_events[key] = now
        await ChannelInboundService._audit(
            db=db,
            user_id=user_id,
            event_type=event_type,
            severity=severity,
            details=details,
        )


__all__ = ["ChannelInboundService", "ChannelNotFound"]
