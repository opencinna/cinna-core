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

    # Pre-wake the env so the live status fetch goes through immediately.
    # The internal StatusUnavailableError → cached snapshot fallback below
    # still covers adapter failures after a successful wake-up, and the
    # "Environment is not running — showing last cached status" defensive
    # message remains accurate as a last-resort fallback.
    requires_running_environment = True

    @property
    def name(self) -> str:
        return "/agent-status"

    @property
    def description(self) -> str:
        return "Show the agent's self-reported status from STATUS.md"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        from app.models.environments.environment import AgentEnvironment
        from app.models.agents.agent import Agent

        with create_session() as db:
            environment = db.get(AgentEnvironment, context.environment_id)
            if not environment:
                return CommandResult(content="Environment not found.", is_error=True)

            agent = db.get(Agent, environment.agent_id)

            snapshot = None
            refresh_command_warning: str | None = None

            # Attempt live fetch, running the configured status-refresh
            # pre-command first (this is a user-initiated live refresh).
            try:
                snapshot = await AgentStatusService.fetch_status(
                    environment, db_session=db, run_refresh_command=True, agent=agent
                )
                refresh_command_warning = snapshot.refresh_command_warning
            except StatusUnavailableError as exc:
                refresh_command_warning = exc.refresh_command_warning
                # Fall back to cached snapshot if one exists
                if environment.status_file_raw or environment.status_file_severity:
                    snapshot = AgentStatusService.get_cached_status(environment)
                    snapshot.refresh_command_warning = refresh_command_warning

            if snapshot is None:
                content = (
                    "No STATUS.md available for this agent.\n\n"
                    "See COMPLEX_AGENT_DESIGN.md for the expected format."
                )
                if refresh_command_warning:
                    content = f"⚠️ _{refresh_command_warning}_\n\n{content}"
                return CommandResult(content=content)

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

            # Status-refresh pre-command warning (non-blocking; surfaced here)
            if refresh_command_warning:
                lines.append(f"⚠️ _{refresh_command_warning}_")

            # Running-state warning: a stopped env can only show cached data
            if environment.status != "running":
                lines.append(
                    "⚠️ _Environment is not running — showing last cached status._"
                )

            lines.append("\n---\n")

            # Prefer `body` (frontmatter stripped) to avoid duplicating the
            # status/summary/timestamp fields rendered above in the header.
            body = snapshot.body if snapshot.body is not None else snapshot.raw
            if body:
                lines.append(body)

            return CommandResult(content="\n".join(lines))
