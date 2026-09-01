"""Channel control commands — text a sender types *at the pipeline*, not at the agent.

Almost everything a person writes into a bound channel thread is for the
agent. A very small set of strings is not: ``/stop`` is the channel-side
equivalent of the web client's stop button, and it has to be answered by the
pipeline itself because there is nothing else in a chat thread that can carry
a button.

The shape is deliberately the **adapter registry's**: a module-level dict from
the normalized command text to its handler, and one function that answers "is
this text a command?". Adding a second command is one entry here plus one
handler — the inbound pipeline does not change, because it dispatches on the
dict rather than on any particular command.

Three properties this module holds, all of them load-bearing:

* **Exact match only.** :func:`match_control_command` strips and casefolds and
  then asks the dict — it does not prefix-match, and it takes no arguments.
  ``"/stop now"``, ``"/stopx"`` and ``"stop"`` are ordinary messages and reach
  the agent, which is the safe direction: a false negative costs someone one
  retyped command, while a false positive silently eats a message the person
  meant their assistant to read.
* **Authorization is the caller's, with one named exception.** The
  interception point in ``ChannelInboundService.process_inbound`` sits below
  every security gate and below the thread-ownership check; see the comment
  there for the full argument, and never call into this module from a path that
  has not made those checks. The exception is identity consent, which is
  re-read inside :func:`handle_stop` because the check that normally makes it
  per-message lives on the ingest path this module bypasses — see that
  function's docstring.
* **Total.** Every entry point runs as a fire-and-forget background task
  (``_schedule``), so an escaping exception is an unhandled task error and
  nothing else — the sender would simply never learn what happened. Both
  :func:`execute_control_command` and each handler therefore swallow and log.

Note what does **not** need a guard here: the email transport. Its
``inbound.text`` is ``format_email_as_message(...)``'s forwarded wrapper, never
the sender's bare words, so no email can ever equal ``"/stop"`` after a strip.
The zero-behaviour-change promise for that transport is structural rather than
a branch someone has to remember.
"""
from __future__ import annotations

import logging
import uuid
from typing import Protocol

from sqlmodel import Session as DBSession

from app.models import (
    ChannelThreadBinding,
    ServerChannel,
    Session as ChatSession,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelControl]"


class ControlCommandHandler(Protocol):
    """What a registry entry has to be.

    Keyword-only ``binding_id`` and nothing else: a command runs against a
    thread, and everything else it could want — the channel, the session, the
    environment — is reachable from the binding in the handler's own DB
    session. Handlers open that session themselves rather than being handed
    one, because they run on a background task long after the request that
    scheduled them has closed its own.
    """

    async def __call__(self, *, binding_id: uuid.UUID) -> None: ...  # pragma: no cover


async def handle_stop(*, binding_id: uuid.UUID) -> None:
    """``/stop`` — interrupt the stream this thread's session is running.

    **Success is silent, and that is the design, not an omission.**
    ``ChannelOutboundService.handle_stream_interrupted`` subscribes to
    ``STREAM_INTERRUPTED`` and settles the thread's status notice with the
    partial answer plus the stopped marker — that message *is* the
    acknowledgement, and it is the one the reader is already looking at. A
    reply from here would post a second message under it saying the same
    thing.

    Only two shapes get a reply, and they are the same shape from the sender's
    side: there is no session bound to this thread yet (a ``pending_install``
    binding still building its environment), or ``interrupt_stream`` says there
    is nothing to interrupt. Both are "nothing is running" and both get
    ``REPLY_NOTHING_TO_STOP``.

    A *failure* of the interrupt is neither of those and is answered with
    silence. By the time ``interrupt_stream`` can fail on something other than
    ``ValueError`` it has already found the stream and asked
    ``active_streaming_manager`` to stop it, so ``STREAM_INTERRUPTED`` may well
    fire anyway; telling the person "there's nothing running right now" while
    the stopped marker lands beside it is worse than saying nothing and letting
    them try again.

    **``ValueError`` is slightly wider than "no active stream", deliberately.**
    ``interrupt_stream`` also raises it for a detached session (no environment)
    and for ``"Environment not found"`` — an infrastructure fault, not an idle
    thread. All three are collapsed into the one reply because the sender can
    act on exactly the same thing in each case (try again, or ask someone), and
    because distinguishing them would mean this decline started describing the
    server's internals to an external sender. The distinction survives in the
    log, not in the thread.

    **Identity-routed threads re-check consent here**, and that is the one gate
    the interception point upstream cannot cover. Everything else it relies on
    was decided for *this* message (ownership, whitelist, channel policy), but
    the per-message identity consent check lives in
    ``ChannelInboundService._ingest`` — on the ``_continue_thread`` path this
    command deliberately bypasses. Without the re-check below, a sender who has
    since switched ``allow_identity_routing`` off could still interrupt a
    stream in the identity owner's workspace. It is the same condition
    ``_ingest`` applies (``grant is not None and not
    policy.allow_identity_routing``), read from the same two facts, so there is
    one rule rather than two spellings of it.

    What is *not* re-verified is the owner's side — a withdrawn
    ``IdentityBindingAssignment``. That check is
    ``ChannelIngestionService.assert_access``, which needs the agent, the
    sender and a full access policy constructed the way ``_ingest`` builds
    them; reproducing that here to gate an interrupt would duplicate the
    subtlest authorization in the feature for a worst case of one stopped turn
    on the thread the sender demonstrably owns. The next ordinary message on
    that thread is refused by ``assert_access`` as it always was.
    """
    from app.core.db import create_session
    from app.services.server_channels.channel_policy_service import (
        ChannelPolicyService,
    )
    from app.services.sessions.message_service import MessageService

    try:
        with create_session() as db:
            binding = db.get(ChannelThreadBinding, binding_id)
            if binding is None:
                # The thread was unbound between the webhook and this task
                # (uninstall, failed-binding self-heal). Nothing to stop and
                # nowhere to say so — the reply needs a channel and a thread
                # key, both of which are on the row that is gone.
                return
            channel = db.get(ServerChannel, binding.server_channel_id)
            if channel is None:
                return

            # Hoisted to plain values **before** anything that can commit.
            # ``interrupt_stream`` runs adapter HTTP and touches the same
            # session; a later ``binding.thread_key`` would be a lazy reload on
            # a possibly-expired instance, which is the hazard
            # ``channel_outbound_service`` §11a documents. Every read below
            # happens while the instance is demonstrably fresh.
            thread_key = str(binding.thread_key)
            binding_user_id = binding.user_id
            session_id = binding.session_id
            environment_id: uuid.UUID | None = None
            chat_session = None
            if session_id is not None:
                chat_session = db.get(ChatSession, session_id)
                if chat_session is not None:
                    environment_id = chat_session.environment_id

            if session_id is None or chat_session is None:
                # A `pending_install` thread, or one whose session was deleted.
                # Nothing has ever streamed here.
                await _reply_nothing_to_stop(db, channel, thread_key)
                return

            # Identity consent, re-read now — see the docstring. The import is
            # function-level for the same circularity reason as
            # ``_reply_nothing_to_stop``'s.
            from app.services.server_channels.channel_inbound_service import (
                ChannelInboundService,
            )

            grant = ChannelInboundService._resume_identity_grant(chat_session)
            if grant is not None:
                policy = ChannelPolicyService.resolve(db, channel, binding_user_id)
                if not policy.allow_identity_routing:
                    logger.info(
                        "%s /stop on identity-routed binding %s refused — the "
                        "sender has switched identity routing off since the "
                        "thread was created",
                        _LOG_PREFIX,
                        binding_id,
                    )
                    # The same uninformative answer an idle thread gets. Naming
                    # the gate would tell an external sender which one closed,
                    # which is exactly what ``_ingest``'s detail-free decline
                    # avoids.
                    await _reply_nothing_to_stop(db, channel, thread_key)
                    return

            try:
                # Authorized by the caller, as this method's contract requires:
                # the interception point ran after the thread-ownership gate,
                # so the person asking owns the conversation being stopped, and
                # the identity-consent gate above covers the one part of that
                # authorization the interception point could not.
                await MessageService.interrupt_stream(
                    db_session=db,
                    session_id=session_id,
                    environment_id=environment_id,
                )
            except ValueError:
                # The documented "nothing to interrupt" answer — also what a
                # detached session (no environment) produces.
                await _reply_nothing_to_stop(db, channel, thread_key)
                return
            except Exception:  # noqa: BLE001 — see the docstring
                logger.warning(
                    "%s Interrupting session %s for binding %s failed — the "
                    "sender is not told, in case the stop landed anyway",
                    _LOG_PREFIX,
                    session_id,
                    binding_id,
                    exc_info=True,
                )
                return

            # Interrupted. Say nothing: the STREAM_INTERRUPTED subscriber
            # settles the notice with the partial answer + the stopped marker.
            logger.info(
                "%s /stop interrupted session %s (binding %s)",
                _LOG_PREFIX,
                session_id,
                binding_id,
            )
    except Exception:  # noqa: BLE001 — background task, nothing above catches
        logger.warning(
            "%s /stop failed for binding %s",
            _LOG_PREFIX,
            binding_id,
            exc_info=True,
        )


async def _reply_nothing_to_stop(
    db: DBSession, channel: ServerChannel, thread_key: str
) -> None:
    """Post the "nothing running" decline into the thread.

    Function-level import of the inbound service: that module imports this one
    at import time to reach :func:`match_control_command`, so the reverse edge
    has to be deferred. Same dodge ``channel_outbound_service`` uses for the
    relay.
    """
    from app.services.server_channels.channel_inbound_service import (
        REPLY_NOTHING_TO_STOP,
        ChannelInboundService,
    )

    await ChannelInboundService._reply(db, channel, thread_key, REPLY_NOTHING_TO_STOP)


#: The registry. Keys are already normalized (stripped, casefolded) — the
#: lookup in :func:`match_control_command` compares against the normalized
#: input, so an entry that is not itself normalized would simply never match.
CONTROL_COMMANDS: dict[str, ControlCommandHandler] = {
    "/stop": handle_stop,
}


def match_control_command(text: str) -> str | None:
    """The registry key ``text`` is, or ``None`` if it is an ordinary message.

    Strip and casefold, then an exact dict lookup. Returning the *key* rather
    than a bool is what lets the caller hand it straight back to
    :func:`execute_control_command` without re-normalizing.
    """
    if not text:
        return None
    normalized = text.strip().casefold()
    if normalized in CONTROL_COMMANDS:
        return normalized
    return None


async def execute_control_command(command: str, *, binding_id: uuid.UUID) -> None:
    """Run the handler for ``command``. Never raises.

    ``command`` is expected to be a key :func:`match_control_command` returned;
    an unknown one is logged and dropped rather than treated as an error,
    because the only way to reach it is a caller that stopped using the
    matcher — and dropping a command is recoverable where an exploding
    background task is not.
    """
    try:
        handler = CONTROL_COMMANDS.get(command)
        if handler is None:
            logger.warning(
                "%s No handler registered for control command %r",
                _LOG_PREFIX,
                command,
            )
            return
        await handler(binding_id=binding_id)
    except Exception:  # noqa: BLE001 — background task, nothing above catches
        logger.warning(
            "%s Control command %r failed for binding %s",
            _LOG_PREFIX,
            command,
            binding_id,
            exc_info=True,
        )


__all__ = [
    "CONTROL_COMMANDS",
    "ControlCommandHandler",
    "execute_control_command",
    "handle_stop",
    "match_control_command",
]
