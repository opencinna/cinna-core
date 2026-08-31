"""
Command Service - framework for handling slash commands in agent sessions.

Slash commands (e.g., /files) are quick, deterministic commands that don't
require an LLM call. They are executed locally and return markdown responses.
"""
import logging
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from sqlmodel import Session as DbSession

    from app.models import Session as ChatSession
    from app.models.sessions.session import SessionCommandPublic

logger = logging.getLogger(__name__)


@dataclass
class CommandContext:
    """Context passed to command handlers."""
    session_id: UUID
    environment_id: UUID
    agent_id: UUID
    user_id: UUID
    access_token_id: UUID | None = None
    frontend_host: str = ""
    backend_base_url: str = ""


CommandDisplay = Literal["notice", "document"]


@dataclass
class CommandResult:
    """Result from a command execution."""
    content: str
    is_error: bool = False
    routing: str | None = None                      # "command_stream" when handler requests async dispatch
    resolved_command: str | None = None             # Shell command for routing=command_stream
    exec_command_short_name: str | None = None      # Short name for the command
    # How the chat UI should present the output. Command output is not agent
    # speech, so the default is ``notice`` — the centered system block every
    # other ``role="system"`` message renders in. Handlers whose output is a
    # document rather than a confirmation (file listings, status reports,
    # command tables) opt into ``document``, which keeps the wide
    # markdown-rendered panel those outputs need.
    display: CommandDisplay = "notice"


class CommandHandler(ABC):
    """Abstract base class for command handlers."""

    streams: bool = False  # If True, handler queues a pending message instead of returning content
    include_in_llm_context: bool = True  # If False, command output is excluded from <prior_commands> block
    requires_running_environment: bool = False  # If True, session_service will auto-wake the env before dispatch

    @property
    @abstractmethod
    def name(self) -> str:
        """Command name (e.g., '/files')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Short description of the command."""
        ...

    @abstractmethod
    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        """Execute the command and return a result."""
        ...


class CommandService:
    """Static registry and dispatcher for slash commands."""

    _handlers: dict[str, CommandHandler] = {}

    @classmethod
    def register(cls, handler: CommandHandler) -> None:
        """Register a command handler."""
        cls._handlers[handler.name] = handler
        logger.info(f"Registered command handler: {handler.name}")

    @classmethod
    def is_command(cls, content: str) -> bool:
        """Check if a message starts with a registered command.

        Supports both space-separated form ("/run check") and colon form ("/run:check").
        """
        if not content:
            return False
        stripped = content.strip()
        if not stripped.startswith("/"):
            return False
        # Check colon form: "/run:name" → base name is "/run"
        first_token = stripped.split()[0].lower()
        if ":" in first_token:
            base_name = first_token.split(":")[0]
            return base_name in cls._handlers
        return first_token in cls._handlers

    @classmethod
    def parse_command(cls, content: str) -> tuple[str, str]:
        """Parse command name and arguments from message content.

        Handles both "/run check" and "/run:check" forms.
        For colon form, the suffix becomes the args.
        """
        stripped = content.strip()
        parts = stripped.split(maxsplit=1)
        first_token = parts[0].lower()
        if ":" in first_token:
            # Colon form: "/run:check" → name="/run", args="check"
            colon_idx = first_token.index(":")
            name = first_token[:colon_idx]
            colon_suffix = first_token[colon_idx + 1:]
            # Remaining space-separated args (if any) are appended
            space_args = parts[1] if len(parts) > 1 else ""
            args = (colon_suffix + (" " + space_args if space_args else "")).strip()
        else:
            name = first_token
            args = parts[1] if len(parts) > 1 else ""
        return name, args

    @classmethod
    def get_handler(cls, name: str) -> "CommandHandler | None":
        """Return the handler instance for a given command name, or None if not registered."""
        return cls._handlers.get(name)

    @classmethod
    def requires_running_environment(cls, content: str) -> bool:
        """Return True if the command in ``content`` needs a running environment.

        Wraps :meth:`parse_command` + :meth:`get_handler` + the handler's
        ``requires_running_environment`` attribute. Returns False for any input
        that does not resolve to a registered handler — consistent with
        :meth:`is_command` semantics (no handler ⇒ no env requirement).
        """
        if not cls.is_command(content):
            return False
        name, _ = cls.parse_command(content)
        handler = cls._handlers.get(name)
        return bool(handler and handler.requires_running_environment)

    @classmethod
    def list_handlers(cls) -> list[CommandHandler]:
        """Return an ordered list of all registered command handlers."""
        return list(cls._handlers.values())

    @classmethod
    async def execute(cls, content: str, context: CommandContext) -> CommandResult:
        """Dispatch to the appropriate command handler."""
        name, args = cls.parse_command(content)
        handler = cls._handlers.get(name)
        if not handler:
            return CommandResult(
                content=f"Unknown command: `{name}`. Available commands: {', '.join(sorted(cls._handlers.keys()))}",
                is_error=True,
            )
        try:
            return await handler.execute(context, args)
        except Exception as e:
            logger.error(f"Error executing command {name}: {e}", exc_info=True)
            return CommandResult(
                content=f"Error executing `{name}`: {str(e)}",
                is_error=True,
            )

    @classmethod
    async def list_for_session(
        cls,
        db: "DbSession",
        chat_session: "ChatSession",
    ) -> list["SessionCommandPublic"]:
        """Build the autocomplete command list for a session.

        Applies display rules:
        - ``/run`` is hidden (an implementation detail — users discover commands
          via ``/run-list`` and invoke them via ``/run:<name>``).
        - ``/run-list`` is shown only when the agent has CLI commands configured.
        - ``/rebuild-env`` is marked unavailable while any co-tenant session on
          the same environment is streaming.
        - ``/session-improve`` is marked unavailable on guest-share and
          webapp-share sessions — there is no identifiable consenting account
          behind them, so submission would be rejected anyway.
        - Dynamic ``/run:<name>`` entries are appended from the env's CLI
          commands cache (with ``resolved_command`` populated for the tooltip).
        """
        from sqlmodel import select

        from app.models import AgentEnvironment, Session as ChatSessionModel
        from app.models.sessions.session import SessionCommandPublic
        from app.services.agents.cli_commands_service import CLICommandsService
        from app.services.sessions.active_streaming_manager import active_streaming_manager

        # Ensure command handlers are registered before listing them
        import app.services.agents.commands  # noqa: F401

        # Resolve environment + CLI commands cache once — used for the
        # /run-list visibility check and for the dynamic /run:<name> entries.
        cli_commands: list = []
        if chat_session.environment_id:
            environment = db.get(AgentEnvironment, chat_session.environment_id)
            if environment:
                cli_commands = CLICommandsService.get_cached_commands(environment)

        # Determine /rebuild-env availability — unavailable if any session on
        # the same environment is actively streaming (mirrors the check in
        # RebuildEnvCommandHandler).
        is_rebuild_env_available = True
        if chat_session.environment_id:
            try:
                session_ids = set(
                    db.exec(
                        select(ChatSessionModel.id).where(
                            ChatSessionModel.environment_id == chat_session.environment_id
                        )
                    ).all()
                )
                if session_ids:
                    is_rebuild_env_available = not await active_streaming_manager.is_any_session_streaming(
                        session_ids
                    )
            except Exception:
                logger.warning(
                    "Failed to check streaming status for /rebuild-env availability",
                    exc_info=True,
                )
                is_rebuild_env_available = True

        # Determine /session-improve availability — a guest or webapp-share
        # session has no identifiable consenting account behind it, so the
        # eligibility gate would reject the submission (plan §4.4 rule 2).
        is_session_improve_available = (
            chat_session.guest_share_id is None
            and chat_session.webapp_share_id is None
        )

        commands: list[SessionCommandPublic] = []
        for handler in cls.list_handlers():
            if handler.name == "/run":
                continue
            if handler.name == "/run-list" and not cli_commands:
                continue
            is_available = True
            if handler.name == "/rebuild-env":
                is_available = is_rebuild_env_available
            elif handler.name == "/session-improve":
                is_available = is_session_improve_available
            commands.append(
                SessionCommandPublic(
                    name=handler.name,
                    description=handler.description,
                    is_available=is_available,
                )
            )

        for cmd in cli_commands:
            commands.append(
                SessionCommandPublic(
                    name=f"/run:{cmd.name}",
                    description=cmd.description if cmd.description else cmd.command[:80],
                    is_available=True,
                    resolved_command=cmd.command,
                )
            )

        return commands
