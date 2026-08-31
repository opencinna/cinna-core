"""
/session-improve command — share this session with the agent's owner.

The chat-side twin of the session page's "Improve Agent" menu item. Both go
through ``ImprovementRequestService.create_from_session``, so the eligibility
gate, the target resolution, the secret scrub, and the rate limits are identical
whichever surface the user consented from — only ``source`` differs.

The confirmation deliberately **names the recipient**. A slash command has no
consent modal in front of it, so the message the user gets back is the only
place the disclosure can happen; it must say who now has a copy, or say plainly
that nothing left their account.
"""
from typing import Any

from app.core.db import create_session as create_db_session
from app.models.improvement.agent_improvement_request import (
    IMPROVEMENT_SOURCE_COMMAND,
)
from app.services.agents.command_service import (
    CommandContext,
    CommandHandler,
    CommandResult,
)


class SessionImproveCommandHandler(CommandHandler):
    """Handler for /session-improve — submit an improvement request."""

    streams = False
    # Meta-command about the conversation, not part of it: showing the LLM that
    # the user reported it would distort the very session being reported on.
    include_in_llm_context = False
    # Never wakes a container. The transcript comes from persisted messages,
    # and the personal-memory read is opportunistic: if the environment happens
    # to be running it is captured, otherwise the block records
    # ``env_not_running``. Submitting a report must not start billable compute.
    requires_running_environment = False

    @property
    def name(self) -> str:
        return "/session-improve"

    @property
    def description(self) -> str:
        return (
            "Share this session with the agent's owner to help improve it "
            "(add --no-memory to leave your personal memory notes out)"
        )

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.models import Session as ChatSession, User
        from app.services.improvement.improvement_request_service import (
            ImprovementRequestDenied,
            ImprovementRequestService,
        )

        comment, include_memory = _parse_args(args)

        with create_db_session() as db:
            chat_session = db.get(ChatSession, context.session_id)
            if chat_session is None:
                return CommandResult(content="Session not found.", is_error=True)

            requester = db.get(User, context.user_id)
            if requester is None:
                return CommandResult(
                    content="Only the owner of this session can share it.",
                    is_error=True,
                )

            try:
                request = await ImprovementRequestService.create_from_session(
                    db,
                    chat_session,
                    requester,
                    comment=comment,
                    source=IMPROVEMENT_SOURCE_COMMAND,
                    include_memory=include_memory,
                )
            except ImprovementRequestDenied as e:
                return CommandResult(content=e.message, is_error=True)

            return CommandResult(content=_confirmation(request.context or {}))


# The one flag the command takes. Matched as a literal rather than run through
# an argument parser: everything else the user types is their free-text comment,
# and a comment must never be silently eaten by argument parsing.
NO_MEMORY_FLAG = "--no-memory"


def _parse_args(args: str) -> tuple[str | None, bool]:
    """Split ``/session-improve`` arguments into ``(comment, include_memory)``.

    ``--no-memory`` is recognised anywhere in the argument string so the user
    does not have to remember whether it comes before or after their comment.
    """
    raw = (args or "").strip()
    include_memory = True
    if NO_MEMORY_FLAG in raw:
        include_memory = False
        raw = raw.replace(NO_MEMORY_FLAG, " ")
    return (" ".join(raw.split()) or None), include_memory


def _confirmation(context: dict[str, Any]) -> str:
    """The disclosure message — the command's stand-in for the consent modal."""
    recipient = context.get("recipient") or {}
    agent = context.get("agent") or {}

    if not recipient.get("is_shared_externally"):
        return (
            "Improvement request created on your own agent. "
            "Nothing was shared outside your account."
        )

    included = _included_line(context)

    who = recipient.get("owner_display") or "the agent's publisher"
    bundle_id = agent.get("bundle_id")
    version = agent.get("installed_version")

    origin = ""
    if bundle_id:
        origin = f", publisher of `{bundle_id}`"
        if version:
            origin += f" (v{version})"

    return (
        "Improvement request submitted. A copy of this conversation was shared "
        f"with **{who}**{origin}. {included}"
    )


def _included_line(context: dict[str, Any]) -> str:
    """Name the configuration that rode along, so the disclosure is complete.

    The prompts always do. The personal memory area only does when the
    requester left it in *and* the container was reachable — saying "and your
    memory notes" when the read failed would overstate what was shared, which
    is the one error this feature cannot absorb.
    """
    from app.services.improvement.session_snapshot_service import (
        MEMORY_REASON_DECLINED,
    )

    memory = context.get("memory") or {}
    if memory.get("available"):
        return (
            "It includes the agent's prompts and the "
            f"{memory.get('file_count', 0)} personal memory file(s) that shape "
            "its system prompt."
        )
    if memory.get("unavailable_reason") == MEMORY_REASON_DECLINED:
        return (
            "It includes the agent's prompts. Your personal memory notes were "
            "left out."
        )
    return (
        "It includes the agent's prompts. No personal memory notes were "
        "captured."
    )
