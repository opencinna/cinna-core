"""
CLICommandsService — reads and caches agent-declared CLI commands from CLI_COMMANDS.yaml.

The agent (or its scripts) writes /app/workspace/docs/CLI_COMMANDS.yaml to declare named
shell commands that users and A2A clients can invoke directly, without an LLM turn.
This service fetches, parses, and caches the file's content on the AgentEnvironment DB row,
mirroring the AgentStatusService pattern exactly.
"""
import logging
import asyncio
import re
from datetime import datetime, UTC
from typing import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import yaml

from app.models.environments.environment import AgentEnvironment

logger = logging.getLogger(__name__)

# Module-level rate-limit lock: env_id -> last_fetch_at (UTC)
_rate_limit_lock: dict[UUID, datetime] = {}

# Module-level constants
CLI_COMMANDS_FILE_PATH = "docs/CLI_COMMANDS.yaml"
MAX_RAW_BYTES = 64 * 1024           # 64 KB — hard cap on stored content
MAX_COMMANDS = 50                   # Maximum number of commands to parse
MAX_COMMAND_LENGTH = 1024           # Maximum command string length in chars
MAX_DESCRIPTION_LENGTH = 512        # Maximum description length in chars
FORCE_REFRESH_TTL_SECONDS = 30      # 30 second rate limit per environment
NAME_REGEX = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')


class CLICommandsUnavailableError(Exception):
    """Raised when CLI_COMMANDS.yaml cannot be fetched (env not running, file missing, adapter error)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class CLICommandsParseError(Exception):
    """Raised by parse_commands_file when yaml.safe_load fails on malformed YAML."""
    pass


@dataclass
class ParsedCLICommand:
    """A single parsed CLI command entry."""
    name: str           # slug, e.g. "check"
    command: str        # raw shell string, e.g. "uv run /app/workspace/scripts/check.py"
    description: str | None  # optional description, up to 512 chars, or None


class CLICommandsService:
    """Service for reading, parsing, and caching agent-declared CLI commands."""

    # ------------------------------------------------------------------ #
    # Rate-limit helpers                                                   #
    # ------------------------------------------------------------------ #

    @classmethod
    def is_rate_limited(cls, environment_id: UUID) -> bool:
        """Return True if this environment was fetched within the last 30 seconds."""
        last = _rate_limit_lock.get(environment_id)
        if last is None:
            return False
        return (datetime.now(UTC) - last).total_seconds() < FORCE_REFRESH_TTL_SECONDS

    @classmethod
    def _mark_rate_limit(cls, environment_id: UUID) -> None:
        _rate_limit_lock[environment_id] = datetime.now(UTC)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def fetch_commands(
        cls, environment: AgentEnvironment, db_session=None
    ) -> list[ParsedCLICommand]:
        """
        Download docs/CLI_COMMANDS.yaml via the environment adapter, parse it, persist the
        cache to the DB, and return the parsed command list.

        Raises CLICommandsUnavailableError when the env is unreachable or the file is missing.

        db_session: optional SQLModel DB session; if None a new one is opened.
        """
        from app.services.environments.environment_service import EnvironmentService

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        # ── Fetch file with metadata ──────────────────────────────────── #
        try:
            meta, stream = await adapter.fetch_workspace_item_with_meta(CLI_COMMANDS_FILE_PATH)
        except Exception as exc:
            logger.warning(
                "cli_commands_fetch_failure agent_id=%s env_id=%s reason=adapter_error: %s",
                environment.agent_id, environment.id, exc,
            )
            cls._persist_error(environment, "adapter_error", db_session)
            raise CLICommandsUnavailableError(f"adapter_error: {exc}")

        if not meta.exists:
            logger.debug(
                "cli_commands_fetch_failure agent_id=%s env_id=%s reason=file_missing",
                environment.agent_id, environment.id,
            )
            cls._persist_error(environment, "file_missing", db_session)
            raise CLICommandsUnavailableError("file_missing")

        # ── Consume bounded body ──────────────────────────────────────── #
        raw_text = await cls._consume_download_stream(stream)

        # ── Parse commands ────────────────────────────────────────────── #
        old_raw = environment.cli_commands_raw
        try:
            commands = cls.parse_commands_file(raw_text)
        except CLICommandsParseError:
            logger.warning(
                "cli_commands_parse_error agent_id=%s env_id=%s",
                environment.agent_id, environment.id,
            )
            cls._persist_error(environment, "parse_error", db_session)
            cls._fire_commands_updated_event(environment, datetime.now(UTC), command_count=0)
            return []

        now = datetime.now(UTC)

        # ── Persist to DB on success ──────────────────────────────────── #
        def _persist(sess):
            env = sess.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.cli_commands_raw = raw_text
            env.cli_commands_parsed = [
                {"name": cmd.name, "command": cmd.command, "description": cmd.description}
                for cmd in commands
            ]
            env.cli_commands_fetched_at = now
            env.cli_commands_error = None
            sess.add(env)
            sess.commit()

        if db_session is not None:
            _persist(db_session)
        else:
            from app.core.db import create_session
            with create_session() as sess:
                _persist(sess)

        cls._mark_rate_limit(environment.id)

        # ── Structured log ────────────────────────────────────────────── #
        logger.info(
            "cli_commands_fetch_success agent_id=%s env_id=%s count=%d",
            environment.agent_id, environment.id, len(commands),
        )

        # ── Emit event when content changes ───────────────────────────── #
        if raw_text != old_raw:
            cls._fire_commands_updated_event(environment, now, command_count=len(commands))

        return commands

    @classmethod
    def get_cached_commands(cls, environment: AgentEnvironment) -> list[ParsedCLICommand]:
        """Return cached command list from the DB row without calling the adapter."""
        if environment.cli_commands_parsed is None:
            return []
        result = []
        for entry in environment.cli_commands_parsed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            command = entry.get("command")
            if not name or not command:
                continue
            result.append(ParsedCLICommand(
                name=name,
                command=command,
                description=entry.get("description"),
            ))
        return result

    @staticmethod
    def parse_commands_file(raw: str) -> list[ParsedCLICommand]:
        """
        Parse a CLI_COMMANDS.yaml string into a list of ParsedCLICommand objects.

        This is a pure static method — no DB or adapter access.
        Raises CLICommandsParseError if yaml.safe_load raises (malformed YAML).
        Returns an empty list for valid YAML with no commands.
        """
        # ── Parse YAML ────────────────────────────────────────────────── #
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise CLICommandsParseError(f"YAML parse error: {exc}") from exc

        # ── Validate top-level structure ──────────────────────────────── #
        if not isinstance(data, dict):
            logger.warning("cli_commands_parse: top-level is not a mapping, treating as empty")
            return []

        raw_commands = data.get("commands")
        if raw_commands is None:
            logger.warning("cli_commands_parse: no 'commands' key found, treating as empty")
            return []

        if not isinstance(raw_commands, list):
            logger.warning("cli_commands_parse: 'commands' is not a list, treating as empty")
            return []

        # ── Parse entries ─────────────────────────────────────────────── #
        results: list[ParsedCLICommand] = []
        seen_names: set[str] = set()

        for entry in raw_commands:
            if len(results) >= MAX_COMMANDS:
                logger.warning(
                    "cli_commands_parse: reached %d command limit, stopping early", MAX_COMMANDS
                )
                break

            if not isinstance(entry, dict):
                logger.warning("cli_commands_parse: skipping non-dict entry: %r", entry)
                continue

            # Validate name
            name = entry.get("name")
            if not name:
                logger.warning("cli_commands_parse: skipping entry with missing 'name'")
                continue
            name = str(name)
            if not NAME_REGEX.match(name):
                logger.warning(
                    "cli_commands_parse: skipping entry with invalid name slug: %r", name
                )
                continue
            if name in seen_names:
                logger.debug("cli_commands_parse: skipping duplicate name: %r", name)
                continue

            # Validate command
            command = entry.get("command")
            if not command:
                logger.warning("cli_commands_parse: skipping entry '%s' with missing 'command'", name)
                continue
            command = str(command).strip()
            if not command:
                logger.warning("cli_commands_parse: skipping entry '%s' with empty command after trim", name)
                continue
            if len(command) > MAX_COMMAND_LENGTH:
                logger.warning(
                    "cli_commands_parse: skipping entry '%s' — command exceeds %d chars",
                    name, MAX_COMMAND_LENGTH,
                )
                continue

            # Validate/truncate description
            description = entry.get("description")
            if description is not None:
                description = str(description)
                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH]

            seen_names.add(name)
            results.append(ParsedCLICommand(name=name, command=command, description=description))

        return results

    # ------------------------------------------------------------------ #
    # Post-action refresh + event handler                                  #
    # ------------------------------------------------------------------ #

    @classmethod
    async def refresh_after_action(
        cls,
        environment: AgentEnvironment,
        db_session=None,
        force: bool = False,
    ) -> None:
        """
        Pull CLI_COMMANDS.yaml after the backend completes an action that ran inside
        the agent-env. Skipped when the per-env rate-limit window is still active,
        unless ``force=True`` (used when the watcher explicitly reports the file changed).

        Best-effort: never raises. Failures are logged at debug level.
        """
        if not force and cls.is_rate_limited(environment.id):
            return
        try:
            await cls.fetch_commands(environment, db_session=db_session)
        except CLICommandsUnavailableError:
            pass  # env not running, file missing, adapter error — all normal
        except Exception as exc:
            logger.debug(
                "cli_commands refresh_after_action failed for env %s: %s",
                environment.id, exc,
            )

    @classmethod
    async def handle_post_action_event(cls, event_data: dict) -> None:
        """
        Generic event handler: pulls CLI_COMMANDS.yaml whenever the backend finishes
        triggering work inside the agent-env. Registered against
        ENVIRONMENT_ACTIVATED, STREAM_COMPLETED / STREAM_ERROR (session streams),
        CRON_COMPLETED_OK / CRON_TRIGGER_SESSION / CRON_ERROR (scheduler), and
        WORKSPACE_FILES_CHANGED (env-core file watcher).

        When the event names docs/CLI_COMMANDS.yaml in ``meta.changed_files`` we
        have direct evidence the cache is stale, so the rate limit is bypassed
        — this is the same auto-sync semantics prompt files use via
        ``EnvironmentService.handle_workspace_files_changed_event``. For every
        other trigger event the fetch is speculative and the 30 s rate limit
        applies.
        """
        try:
            meta = event_data.get("meta", {}) or {}
            environment_id = meta.get("environment_id")
            if not environment_id:
                return
            changed_files = meta.get("changed_files") or []
            force = CLI_COMMANDS_FILE_PATH in changed_files
            from app.core.db import create_session as _create_session
            with _create_session() as session:
                env = session.get(AgentEnvironment, UUID(environment_id))
                if env is None:
                    return
                await cls.refresh_after_action(env, db_session=session, force=force)
        except Exception as exc:
            logger.debug("cli_commands handle_post_action_event swallowed: %s", exc)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def _persist_error(
        cls,
        environment: AgentEnvironment,
        error_reason: str,
        db_session=None,
    ) -> None:
        """Persist an error reason to the DB row without updating parsed content."""
        def _do_persist(sess):
            env = sess.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.cli_commands_error = error_reason
            sess.add(env)
            sess.commit()

        try:
            if db_session is not None:
                _do_persist(db_session)
            else:
                from app.core.db import create_session
                with create_session() as sess:
                    _do_persist(sess)
        except Exception as exc:
            logger.debug("cli_commands _persist_error failed: %s", exc)

    @classmethod
    async def _consume_download_stream(cls, stream: AsyncIterator[bytes]) -> str:
        """Read an async byte stream into a string, capped at MAX_RAW_BYTES."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > MAX_RAW_BYTES:
                over = total - MAX_RAW_BYTES
                safe_chunk = chunk[:-over] if over < len(chunk) else b""
                chunks.append(safe_chunk)
                return (
                    b"".join(chunks).decode("utf-8", errors="replace")
                    + "\n... (truncated)"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    @classmethod
    def _fire_commands_updated_event(
        cls,
        environment: AgentEnvironment,
        fetched_at: datetime,
        command_count: int,
    ) -> None:
        """Emit cli_commands_updated event via the event bus (best-effort; never raises)."""
        try:
            from app.services.events.event_service import event_service
            from app.models.events.event import EventType
            from app.core.db import create_session
            from app.models.agents.agent import Agent as AgentModel

            owner_id = None
            with create_session() as sess:
                agent = sess.get(AgentModel, environment.agent_id)
                if agent:
                    owner_id = agent.owner_id

            if owner_id is None:
                return

            async def _emit() -> None:
                await event_service.emit_event(
                    event_type=EventType.CLI_COMMANDS_UPDATED,
                    model_id=environment.agent_id,
                    user_id=owner_id,
                    meta={
                        "agent_id": str(environment.agent_id),
                        "environment_id": str(environment.id),
                        "fetched_at": fetched_at.isoformat(),
                        "command_count": command_count,
                    },
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_emit())
            except RuntimeError:
                pass  # no running event loop in sync context
        except Exception as exc:
            logger.debug("Failed to emit cli_commands_updated event: %s", exc)
