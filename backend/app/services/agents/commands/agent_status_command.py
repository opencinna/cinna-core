"""
/agent-status command — returns the agent's self-reported status from STATUS.md.

Bypasses the LLM pipeline entirely. Attempts a live fetch first; falls back to
the cached DB snapshot when the environment is not running or the file is absent.
"""
import logging

from app.services.agents.command_service import CommandHandler, CommandContext, CommandResult
from app.services.agents.agent_status_service import AgentStatusService, StatusUnavailableError
from app.core.db import create_session

logger = logging.getLogger(__name__)

SEVERITY_ICONS: dict[str, str] = {
    "ok": "🟢",
    "info": "🔵",
    "warning": "🟡",
    "error": "🔴",
    "unknown": "⚪",
}


class AgentStatusCommandHandler(CommandHandler):
    """Handler for /agent-status — shows the agent's self-reported STATUS.md content."""

    @property
    def name(self) -> str:
        return "/agent-status"

    @property
    def description(self) -> str:
        return "Show the agent's self-reported status from STATUS.md"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.models.environments.environment import AgentEnvironment

        with create_session() as db:
            environment = db.get(AgentEnvironment, context.environment_id)
            if not environment:
                return CommandResult(content="Environment not found.", is_error=True)

            snapshot = None

            # Attempt live fetch
            try:
                snapshot = await AgentStatusService.fetch_status(environment)
            except StatusUnavailableError:
                # Fall back to cached snapshot if one exists
                if environment.status_file_raw or environment.status_file_severity:
                    snapshot = AgentStatusService.get_cached_status(environment)

            if snapshot is None:
                return CommandResult(
                    content=(
                        "No STATUS.md available for this agent.\n\n"
                        "See COMPLEX_AGENT_DESIGN.md for the expected format."
                    )
                )

            # Build markdown response
            severity = snapshot.severity or "unknown"
            icon = SEVERITY_ICONS.get(severity, "⚪")
            summary = snapshot.summary or "_No summary_"
            lines = [f"**Status:** {icon} {severity.upper()} — {summary}"]

            # Timestamp line
            ts_parts = []
            if snapshot.reported_at:
                ts_parts.append(
                    f"Reported {snapshot.reported_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                )
            if snapshot.fetched_at:
                ts_parts.append(
                    f"fetched {snapshot.fetched_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                )
            if ts_parts:
                lines.append(f"_{' · '.join(ts_parts)}_")

            # Transition info (only when severity actually changed)
            if snapshot.prev_severity and snapshot.prev_severity != severity:
                prev_icon = SEVERITY_ICONS.get(snapshot.prev_severity, "⚪")
                lines.append(f"_Changed from {prev_icon} {snapshot.prev_severity}_")

            # Stale warning
            if snapshot.is_stale:
                if environment.status != "running":
                    stale_msg = "Environment is not running — showing last cached status."
                else:
                    stale_msg = "Status may be outdated."
                lines.append(f"⚠️ _{stale_msg}_")

            lines.append("\n---\n")

            if snapshot.raw:
                lines.append(snapshot.raw)

            return CommandResult(content="\n".join(lines))
