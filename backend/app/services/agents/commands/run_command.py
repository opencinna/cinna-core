"""
/run command — executes a named CLI command defined in docs/CLI_COMMANDS.yaml.

List mode (/run with no args): returns a markdown table of available commands.
Exec mode (/run:<name> or /run <name>): queues the command for streaming execution
via the command_stream routing path through SessionStreamProcessor.
"""
import logging
import re

from app.services.agents.command_service import CommandHandler, CommandContext, CommandResult

logger = logging.getLogger(__name__)

# Validation regex for command short names
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


async def _list_cli_commands(context: CommandContext) -> CommandResult:
    """Return a markdown table of CLI commands declared in CLI_COMMANDS.yaml.

    Shared by ``/run`` (no-args list mode) and ``/run-list`` — keeps both
    entry points in lockstep without one reaching into the other's privates.
    """
    from app.core.db import create_session
    from app.models import AgentEnvironment
    from app.services.agents.cli_commands_service import CLICommandsService

    with create_session() as db:
        environment = db.get(AgentEnvironment, context.environment_id)
        if not environment:
            return CommandResult(
                content="Environment not found.",
                is_error=True,
            )
        commands = CLICommandsService.get_cached_commands(environment)

    if not commands:
        return CommandResult(
            content=(
                "No commands configured. "
                "Create `docs/CLI_COMMANDS.yaml` in your workspace to define commands."
            ),
            is_error=True,
        )

    lines = ["| Name | Command | Description |", "|------|---------|-------------|"]
    for cmd in commands:
        desc = cmd.description or ""
        lines.append(f"| `{cmd.name}` | `{cmd.command[:80]}` | {desc} |")

    return CommandResult(content="\n".join(lines), display="document")


def _find_command(commands, name: str):
    """Case-insensitive lookup of a command by name."""
    name_lower = name.lower()
    for cmd in commands:
        if cmd.name.lower() == name_lower:
            return cmd
    return None


class RunCommandHandler(CommandHandler):
    """Handler for /run — executes named CLI commands from CLI_COMMANDS.yaml."""

    streams: bool = True  # Exec mode queues a pending message for async streaming

    @property
    def name(self) -> str:
        return "/run"

    @property
    def description(self) -> str:
        return "Execute a named CLI command defined in CLI_COMMANDS.yaml (or list all with /run)"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        """Execute or list run commands.

        Args:
            context: Command context with session/environment/agent/user IDs.
            args: Empty string for list mode; command name for exec mode.

        Returns:
            CommandResult with routing=None for list mode (synchronous),
            or routing="command_stream" for exec mode (queued async streaming).
        """
        name = args.strip().lstrip(":").strip()

        if not name:
            return await _list_cli_commands(context)

        # Validate name format
        if not _NAME_RE.match(name):
            return CommandResult(
                content=(
                    f"Invalid command name `{name}`. "
                    "Names must be 1–64 characters: letters, digits, underscores, hyphens."
                ),
                is_error=True,
            )

        return await self._handle_exec_mode(context, name)

    async def _handle_exec_mode(self, context: CommandContext, name: str) -> CommandResult:
        """Queue a command for streaming execution."""
        from app.core.db import create_session
        from app.models import AgentEnvironment
        from app.models.sessions.session import Session as ChatSession
        from app.services.agents.cli_commands_service import CLICommandsService

        with create_session() as db:
            # Check for guest session — command execution is not allowed
            chat_session = db.get(ChatSession, context.session_id)
            if chat_session and chat_session.guest_share_id is not None:
                return CommandResult(
                    content="Command execution is not available in guest sessions.",
                    is_error=True,
                )

            environment = db.get(AgentEnvironment, context.environment_id)
            if not environment:
                return CommandResult(
                    content="Environment not found.",
                    is_error=True,
                )

            commands = CLICommandsService.get_cached_commands(environment)
            entry = _find_command(commands, name)

            if entry is None and environment.cli_commands_parsed:
                # Cache hit but name not found — try a stale-cache refresh
                logger.info(
                    "run_command: '%s' not found in cache for env %s, attempting refresh",
                    name, context.environment_id,
                )
                try:
                    refreshed = await CLICommandsService.fetch_commands(environment, db_session=db)
                    entry = _find_command(refreshed, name)
                except Exception as exc:
                    logger.warning("run_command: refresh failed: %s", exc)

            if entry is None:
                available = [cmd.name for cmd in CLICommandsService.get_cached_commands(environment)]
                if available:
                    available_str = ", ".join(f"`{n}`" for n in available)
                    return CommandResult(
                        content=f"Unknown command `{name}`. Available: {available_str}",
                        is_error=True,
                    )
                return CommandResult(
                    content=(
                        f"Unknown command `{name}`. "
                        "No commands are configured in `docs/CLI_COMMANDS.yaml`."
                    ),
                    is_error=True,
                )

        return CommandResult(
            content="",
            routing="command_stream",
            resolved_command=entry.command,
            exec_command_short_name=entry.name,
        )


class RunListCommandHandler(CommandHandler):
    """Handler for /run-list — lists CLI commands declared in CLI_COMMANDS.yaml.

    Delegates to the shared ``_list_cli_commands`` helper so the autocomplete
    popup can surface a dedicated list entry (shown only when the agent has
    at least one CLI command configured).
    """

    streams: bool = False

    @property
    def name(self) -> str:
        return "/run-list"

    @property
    def description(self) -> str:
        return "List CLI commands exposed via docs/CLI_COMMANDS.yaml"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        if args.strip():
            return CommandResult(
                content="`/run-list` takes no arguments. Use `/run:<name>` to execute a command.",
                is_error=True,
            )
        return await _list_cli_commands(context)
