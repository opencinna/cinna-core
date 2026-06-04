"""
AgentStatusService — reads and caches agent self-reported status from STATUS.md.

The agent (or its scripts) writes /app/workspace/app-data/storage/STATUS.md whenever
its state changes. The file lives in App Data (per-user, per-bundle persistent
storage) rather than in the bundle-owned ``docs/`` tree because status is a
property of the running install instance, not part of the published bundle. This
service reads the file, parses the optional YAML frontmatter, persists the
snapshot to the AgentEnvironment DB row, and emits events on severity
transitions.
"""
import logging
import asyncio
from datetime import datetime, UTC
from typing import AsyncIterator, TYPE_CHECKING
from dataclasses import dataclass
from uuid import UUID

import yaml

from app.models.environments.environment import AgentEnvironment

if TYPE_CHECKING:
    from app.models.agents.agent import Agent

logger = logging.getLogger(__name__)

# Module-level rate-limit lock: env_id -> last_fetch_at (UTC)
_rate_limit_lock: dict[UUID, datetime] = {}


class StatusUnavailableError(Exception):
    """Raised when STATUS.md cannot be fetched (env not running, file missing, adapter error)."""

    def __init__(self, reason: str, refresh_command_warning: str | None = None):
        self.reason = reason
        # Carries the status-refresh pre-command warning across the failed
        # download so callers can attach it to the cached fallback snapshot.
        self.refresh_command_warning = refresh_command_warning
        super().__init__(reason)


@dataclass
class ParsedStatus:
    """Result of parsing a STATUS.md file."""
    severity: str          # ok / warning / error / info / unknown
    summary: str | None
    reported_at: datetime | None    # from frontmatter timestamp only (not mtime)
    raw_body: str
    has_structured_metadata: bool


@dataclass
class AgentStatusSnapshot:
    """Full status snapshot ready for API response or DB persistence."""
    agent_id: UUID | None
    environment_id: UUID | None
    severity: str | None
    summary: str | None
    reported_at: datetime | None
    reported_at_source: str | None   # "frontmatter" | "file_mtime" | None
    fetched_at: datetime | None
    raw: str | None
    body: str | None                 # raw minus the leading YAML frontmatter block
    has_structured_metadata: bool
    prev_severity: str | None
    severity_changed_at: datetime | None
    # Transient warning from the status-refresh pre-command (never persisted).
    # Populated only on force/live refreshes when the pre-command did not run
    # cleanly; None on cache-only snapshots.
    refresh_command_warning: str | None = None


class AgentStatusService:
    """Service for reading, parsing, and caching agent self-reported status."""

    STATUS_FILE_PATH = "app-data/storage/STATUS.md"
    MAX_RAW_BYTES = 64 * 1024        # 64 KB — hard cap on stored content
    MAX_FRONTMATTER_BYTES = 4 * 1024  # 4 KB — oversized frontmatter falls through
    SEVERITY_VALUES = {"ok", "warning", "error", "info"}
    FORCE_REFRESH_TTL_SECONDS = 30   # 30 second rate limit per environment
    STATUS_REFRESH_COMMAND_TIMEOUT = 120  # seconds — bounds the pre-command exec

    # ------------------------------------------------------------------ #
    # Rate-limit helpers                                                   #
    # ------------------------------------------------------------------ #

    @classmethod
    def is_rate_limited(cls, environment_id: UUID) -> bool:
        """Return True if this environment was fetched within the last 30 seconds."""
        last = _rate_limit_lock.get(environment_id)
        if last is None:
            return False
        return (datetime.now(UTC) - last).total_seconds() < cls.FORCE_REFRESH_TTL_SECONDS

    @classmethod
    def _mark_rate_limit(cls, environment_id: UUID) -> None:
        _rate_limit_lock[environment_id] = datetime.now(UTC)

    # ------------------------------------------------------------------ #
    # Status refresh pre-command                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def _ensure_environment_running(
        cls, environment: AgentEnvironment, agent: "Agent | None"
    ) -> None:
        """
        On user-initiated force refresh, wake a suspended environment so the
        status pre-command and the STATUS.md fetch can actually run instead of
        serving a stale cached snapshot. Mirrors the auto-resume behavior of the
        message-send / CLI / session paths.

        No-op when the env is already running, when no agent is available, or for
        statuses other than ``suspended`` (``stopped`` / ``error`` envs keep the
        existing "environment not running" warning path — they need a heavier
        full start that a status poll shouldn't trigger).

        Best-effort: any activation failure is swallowed so the caller falls back
        to the normal not-running handling. Because activation rotates the env's
        auth token, the refreshed ``status`` and ``config`` are copied back onto
        the passed ``environment`` so the downstream exec/fetch use current
        values. Concurrency-safe: if a parallel request already woke (or is
        waking) the env, the latest DB status/config is still copied back.
        """
        if environment.status == "running" or agent is None:
            return
        if environment.status != "suspended":
            return

        from app.services.environments.environment_lifecycle import (
            EnvironmentLifecycleManager,
        )
        from app.models.agents.agent import Agent as AgentModel
        from app.core.db import create_session

        try:
            lifecycle = EnvironmentLifecycleManager()
            with create_session() as sess:
                fresh_env = sess.get(AgentEnvironment, environment.id)
                if fresh_env is None:
                    return
                if fresh_env.status == "suspended":
                    fresh_agent = sess.get(AgentModel, fresh_env.agent_id)
                    if fresh_agent is None:
                        return
                    logger.info(
                        "agent_status_resume_environment agent_id=%s env_id=%s "
                        "action=activating",
                        environment.agent_id, environment.id,
                    )
                    await lifecycle.activate_suspended_environment(
                        db_session=sess,
                        environment=fresh_env,
                        agent=fresh_agent,
                        emit_events=True,
                    )
                # Copy the latest status/config back onto the caller's instance
                # (covers both our own activation and a concurrent one).
                environment.status = fresh_env.status
                environment.config = fresh_env.config
        except Exception as exc:
            logger.warning(
                "agent_status_resume_environment agent_id=%s env_id=%s "
                "action=failed reason=%s",
                environment.agent_id, environment.id, exc,
            )

    @classmethod
    async def _run_refresh_command(
        cls, agent: "Agent | None", environment: AgentEnvironment
    ) -> str | None:
        """
        Run the agent's configured status-refresh pre-command inside the
        container before a live status fetch.

        Resolves ``agent.status_refresh_command``:
          - blank/None → no command configured, returns None (no warning).
          - "/run:<name>" → resolved against ``environment.cli_commands_parsed``;
            a missing name returns a warning and skips execution.
          - any other value → used verbatim as the shell command.

        Non-blocking by contract: never raises. Any failure (env down, missing
        reference, non-zero exit, timeout, connect error, unexpected error) is
        converted to a generic warning string that the caller surfaces in the
        status snapshot. Warnings deliberately exclude stdout/stderr so command
        output can never leak into the public-ish status surfaces.

        Returns a warning string when the command did not run cleanly, or None
        when it ran successfully or there was nothing to run.
        """
        raw_command = (agent.status_refresh_command or "").strip() if agent else ""
        if not raw_command:
            return None  # deliberate opt-out — no command, no warning

        resolved_form = "shell"
        if raw_command.startswith("/run:"):
            resolved_form = "run"
            name = raw_command[len("/run:"):].strip()
            command = cls._resolve_run_command(environment, name)
            if command is None:
                logger.info(
                    "agent_status_refresh_command agent_id=%s env_id=%s "
                    "resolved_form=run name=%s outcome=warning reason=not_found",
                    environment.agent_id, environment.id, name,
                )
                return (
                    f"Refresh command '/run:{name}' is not defined in "
                    "CLI_COMMANDS.yaml — skipped."
                )
        else:
            command = raw_command

        if environment.status != "running":
            logger.info(
                "agent_status_refresh_command agent_id=%s env_id=%s "
                "resolved_form=%s outcome=warning reason=env_not_running",
                environment.agent_id, environment.id, resolved_form,
            )
            return "Environment is not running — refresh command skipped."

        from app.services.sessions.message_service import MessageService
        from app.services.environments.agent_env_connector import AgentEnvConnector

        base_url = MessageService.get_environment_url(environment)
        auth_token = (environment.config or {}).get("auth_token", "")

        try:
            result = await AgentEnvConnector().exec_command(
                base_url=base_url,
                auth_token=auth_token,
                command=command,
                timeout=cls.STATUS_REFRESH_COMMAND_TIMEOUT,
            )
        except RuntimeError as exc:
            # exec_command raises RuntimeError on HTTP error / timeout / connect
            # failure. The exception message is generic (no stdout/stderr).
            logger.info(
                "agent_status_refresh_command agent_id=%s env_id=%s "
                "resolved_form=%s outcome=warning reason=exec_failed: %s",
                environment.agent_id, environment.id, resolved_form, exc,
            )
            return f"Refresh command failed: {exc}"
        except Exception as exc:
            logger.warning(
                "agent_status_refresh_command agent_id=%s env_id=%s "
                "resolved_form=%s outcome=warning reason=unexpected: %s",
                environment.agent_id, environment.id, resolved_form, exc,
            )
            return "Refresh command failed unexpectedly — skipped."

        exit_code = result.get("exit_code", -1)
        if exit_code != 0:
            logger.info(
                "agent_status_refresh_command agent_id=%s env_id=%s "
                "resolved_form=%s exit=%s outcome=warning",
                environment.agent_id, environment.id, resolved_form, exit_code,
            )
            return f"Refresh command exited with code {exit_code}."

        logger.info(
            "agent_status_refresh_command agent_id=%s env_id=%s "
            "resolved_form=%s exit=0 outcome=ok",
            environment.agent_id, environment.id, resolved_form,
        )
        return None

    @staticmethod
    def _resolve_run_command(
        environment: AgentEnvironment, name: str
    ) -> str | None:
        """
        Look up a ``/run:<name>`` reference in the environment's cached
        CLI_COMMANDS.yaml list. Returns the resolved shell command string, or
        None when the name is not present.
        """
        if not name:
            return None
        for entry in environment.cli_commands_parsed or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                command = entry.get("command")
                return command if command else None
        return None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def fetch_status(
        cls,
        environment: AgentEnvironment,
        db_session=None,
        *,
        run_refresh_command: bool = False,
        agent: "Agent | None" = None,
    ) -> AgentStatusSnapshot:
        """
        Download app-data/storage/STATUS.md via the environment adapter, parse it, persist the
        snapshot to the DB, and return an AgentStatusSnapshot.

        Raises StatusUnavailableError when the env is unreachable, the file is
        missing, or the adapter raises an error.

        db_session: optional SQLModel DB session; if None a new one is opened.

        run_refresh_command: when True (force/live refresh paths only), run the
            agent's configured status-refresh pre-command before downloading the
            file. Non-blocking — any pre-command failure becomes a transient
            ``refresh_command_warning`` on the returned snapshot (and is stashed
            on a raised StatusUnavailableError so the caller's cached fallback
            can surface it). The post-action / event-driven refresh path leaves
            this False so background refreshes never run the command.
        agent: optional pre-loaded Agent; resolved by ``environment.agent_id``
            when omitted. Only used when run_refresh_command is True.
        """
        from app.services.environments.environment_service import EnvironmentService

        refresh_command_warning: str | None = None
        if run_refresh_command:
            if agent is None:
                agent = cls._load_agent(environment.agent_id, db_session)
            # User-initiated force refresh wakes a suspended env first (mirrors
            # message-send / CLI auto-resume) so the pre-command and STATUS.md
            # fetch can actually run instead of serving a stale cached snapshot.
            await cls._ensure_environment_running(environment, agent)
            refresh_command_warning = await cls._run_refresh_command(agent, environment)

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        # ── Fetch file with metadata ──────────────────────────────────── #
        try:
            meta, stream = await adapter.fetch_workspace_item_with_meta(cls.STATUS_FILE_PATH)
        except Exception as exc:
            logger.warning(
                "agent_status_fetch_failure agent_id=%s env_id=%s reason=adapter_error: %s",
                environment.agent_id, environment.id, exc,
            )
            raise StatusUnavailableError(
                f"adapter_error: {exc}",
                refresh_command_warning=refresh_command_warning,
            )

        if not meta.exists:
            logger.debug(
                "agent_status_fetch_failure agent_id=%s env_id=%s reason=file_missing",
                environment.agent_id, environment.id,
            )
            raise StatusUnavailableError(
                "file_missing",
                refresh_command_warning=refresh_command_warning,
            )

        # ── Consume bounded body ──────────────────────────────────────── #
        raw_text = await cls._consume_download_stream(stream)

        # ── Parse frontmatter + body ──────────────────────────────────── #
        parsed = cls.parse_status_file(raw_text)

        # ── Resolve reported_at ───────────────────────────────────────── #
        reported_at, reported_at_source = cls._resolve_reported_at(
            parsed.reported_at, meta.modified_at
        )

        now = datetime.now(UTC)
        old_severity = environment.status_file_severity
        new_severity = parsed.severity
        severity_transitioned = old_severity != new_severity

        if severity_transitioned:
            prev_severity = old_severity
            severity_changed_at = now
        else:
            prev_severity = environment.status_file_prev_severity
            severity_changed_at = environment.status_file_severity_changed_at

        old_raw = environment.status_file_raw

        # ── Persist snapshot to DB ────────────────────────────────────── #
        def _persist(sess):
            env = sess.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.status_file_raw = raw_text
            env.status_file_severity = new_severity
            env.status_file_summary = parsed.summary
            env.status_file_reported_at = reported_at
            env.status_file_reported_at_source = reported_at_source
            env.status_file_fetched_at = now
            if severity_transitioned:
                env.status_file_prev_severity = prev_severity
                env.status_file_severity_changed_at = severity_changed_at
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
            "agent_status_fetch_success agent_id=%s env_id=%s severity=%s "
            "reported_at_source=%s transitioned=%s",
            environment.agent_id, environment.id, new_severity,
            reported_at_source, severity_transitioned,
        )

        # ── Emit event on transition or content change ────────────────── #
        content_changed = raw_text != old_raw
        if severity_transitioned or content_changed:
            cls._fire_status_updated_event(
                environment, new_severity, prev_severity,
                parsed.summary, reported_at, reported_at_source, now,
            )

        # ── Create activity entry on severity transition ──────────────── #
        if severity_transitioned:
            try:
                cls._create_transition_activity(environment, old_severity, new_severity)
            except Exception as exc:
                logger.debug("Failed to create status transition activity: %s", exc)

        return AgentStatusSnapshot(
            agent_id=environment.agent_id,
            environment_id=environment.id,
            severity=new_severity,
            summary=parsed.summary,
            reported_at=reported_at,
            reported_at_source=reported_at_source,
            fetched_at=now,
            raw=raw_text,
            body=parsed.raw_body,
            has_structured_metadata=parsed.has_structured_metadata,
            prev_severity=prev_severity,
            severity_changed_at=severity_changed_at,
            refresh_command_warning=refresh_command_warning,
        )

    @classmethod
    async def force_refresh_status(
        cls,
        environment: AgentEnvironment,
        agent: "Agent | None" = None,
        db_session=None,
    ) -> AgentStatusSnapshot:
        """
        Single entrypoint for a user-initiated status refresh.

        This is the one method behind the UI refresh button, the REST
        ``GET /agents/{id}/status?force_refresh=true`` path, the A2A
        ``agent/status`` force path, and the ``/agent-status`` command. It wakes
        a suspended environment, runs the configured status-refresh pre-command,
        fetches and persists STATUS.md, and returns the resulting snapshot.

        Never raises: on adapter failure / missing file it returns the cached DB
        snapshot with any pre-command warning attached, so callers only need to
        shape the response. (Surface-specific concerns — A2A's protocol-level
        rate-limit, the slash command's "no STATUS.md" copy — stay with their
        callers.)
        """
        try:
            return await cls.fetch_status(
                environment,
                db_session=db_session,
                run_refresh_command=True,
                agent=agent,
            )
        except StatusUnavailableError as exc:
            snapshot = cls.get_cached_status(environment)
            snapshot.refresh_command_warning = exc.refresh_command_warning
            return snapshot

    @classmethod
    def _load_agent(cls, agent_id: "UUID | None", db_session=None) -> "Agent | None":
        """Load an Agent by id, reusing the provided session or opening a new one."""
        if agent_id is None:
            return None
        from app.models.agents.agent import Agent as AgentModel

        if db_session is not None:
            return db_session.get(AgentModel, agent_id)
        from app.core.db import create_session
        with create_session() as sess:
            return sess.get(AgentModel, agent_id)

    @classmethod
    def get_cached_status(cls, environment: AgentEnvironment) -> AgentStatusSnapshot:
        """Return a snapshot built from the persisted DB fields without calling the adapter."""
        has_meta = (
            environment.status_file_severity is not None
            or environment.status_file_reported_at is not None
        )
        body = (
            cls.parse_status_file(environment.status_file_raw).raw_body
            if environment.status_file_raw is not None
            else None
        )
        return AgentStatusSnapshot(
            agent_id=environment.agent_id,
            environment_id=environment.id,
            severity=environment.status_file_severity,
            summary=environment.status_file_summary,
            reported_at=environment.status_file_reported_at,
            reported_at_source=environment.status_file_reported_at_source,
            fetched_at=environment.status_file_fetched_at,
            raw=environment.status_file_raw,
            body=body,
            has_structured_metadata=has_meta,
            prev_severity=environment.status_file_prev_severity,
            severity_changed_at=environment.status_file_severity_changed_at,
            refresh_command_warning=None,
        )

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
        Pull STATUS.md after the backend completes an action that ran inside
        the agent-env (session stream, CRON script trigger). Skipped when the
        per-env rate-limit window is still active — another refresher
        (force-refresh REST endpoint or a recent post-action event) already
        pulled within the last 30 s — unless ``force=True``, used when the
        watcher explicitly reports the file changed.

        Best-effort: never raises. Failures are logged at debug level.
        """
        if not force and cls.is_rate_limited(environment.id):
            return
        try:
            await cls.fetch_status(environment, db_session=db_session)
        except StatusUnavailableError:
            pass  # env not running, STATUS.md missing, adapter error
        except Exception as exc:
            logger.debug(
                "agent_status refresh_after_action failed for env %s: %s",
                environment.id, exc,
            )

    @classmethod
    async def handle_post_action_event(cls, event_data: dict) -> None:
        """
        Generic event handler: pulls STATUS.md whenever the backend finishes
        triggering work inside the agent-env. Registered against
        STREAM_COMPLETED / STREAM_ERROR (session streams),
        CRON_COMPLETED_OK / CRON_TRIGGER_SESSION / CRON_ERROR (scheduler), and
        WORKSPACE_FILES_CHANGED (env-core file watcher).

        The agent-env has no outbound access, so the backend is the only
        actor that knows an action just finished — this handler turns that
        knowledge into a fresh snapshot.

        When the event names ``app-data/storage/STATUS.md`` in
        ``meta.changed_files`` we have direct evidence the cache is stale, so
        the rate limit is bypassed — the same auto-sync semantics prompt files
        use. For every other trigger event the fetch is speculative and the
        30 s rate limit applies.
        """
        try:
            meta = event_data.get("meta", {}) or {}
            environment_id = meta.get("environment_id")
            if not environment_id:
                return
            changed_files = meta.get("changed_files") or []
            force = cls.STATUS_FILE_PATH in changed_files
            from app.core.db import create_session as _create_session
            with _create_session() as session:
                env = session.get(AgentEnvironment, UUID(environment_id))
                if env is None:
                    return
                await cls.refresh_after_action(env, db_session=session, force=force)
        except Exception as exc:
            logger.debug("agent_status handle_post_action_event swallowed: %s", exc)

    # ------------------------------------------------------------------ #
    # Environment helpers                                                  #
    # ------------------------------------------------------------------ #

    @classmethod
    def get_primary_environment(
        cls,
        session,
        agent_id: UUID,
        active_env_id: "UUID | None",
    ) -> "AgentEnvironment | None":
        """Return the primary environment for an agent (active first, then latest by updated_at)."""
        from sqlmodel import select
        if active_env_id:
            env = session.get(AgentEnvironment, active_env_id)
            if env:
                return env
        stmt = (
            select(AgentEnvironment)
            .where(AgentEnvironment.agent_id == agent_id)
            .order_by(AgentEnvironment.updated_at.desc())
        )
        return session.exec(stmt).first()

    @classmethod
    def empty_snapshot(cls, agent_id: UUID) -> "AgentStatusSnapshot":
        """Return a sentinel snapshot for agents with no environment."""
        return AgentStatusSnapshot(
            agent_id=agent_id,
            environment_id=None,
            severity=None,
            summary=None,
            reported_at=None,
            reported_at_source=None,
            fetched_at=None,
            raw=None,
            body=None,
            has_structured_metadata=False,
            prev_severity=None,
            severity_changed_at=None,
            refresh_command_warning=None,
        )

    # ------------------------------------------------------------------ #
    # Parser                                                               #
    # ------------------------------------------------------------------ #

    @classmethod
    def parse_status_file(cls, content: str) -> ParsedStatus:
        """Parse optional YAML frontmatter + freeform body. Returns ParsedStatus."""
        frontmatter, body = cls._parse_frontmatter(content)

        severity = "unknown"
        summary = None
        reported_at = None
        has_structured_metadata = False

        if frontmatter is not None:
            raw_status = frontmatter.get("status")
            raw_summary = frontmatter.get("summary")
            raw_timestamp = frontmatter.get("timestamp")

            if raw_status is not None or raw_timestamp is not None:
                has_structured_metadata = True

            if raw_status is not None:
                severity = cls._normalize_severity(str(raw_status))

            if raw_summary is not None:
                summary = str(raw_summary)[:512]

            if raw_timestamp is not None:
                try:
                    if isinstance(raw_timestamp, datetime):
                        reported_at = raw_timestamp
                        if reported_at.tzinfo is None:
                            reported_at = reported_at.replace(tzinfo=UTC)
                    else:
                        reported_at = datetime.fromisoformat(str(raw_timestamp))
                        if reported_at.tzinfo is None:
                            reported_at = reported_at.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    reported_at = None

        if summary is None:
            summary = cls._extract_fallback_summary(body)

        return ParsedStatus(
            severity=severity,
            summary=summary,
            reported_at=reported_at,
            raw_body=body,
            has_structured_metadata=has_structured_metadata,
        )

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_severity(raw: str | None) -> str:
        """Return one of ok/warning/error/info, or 'unknown' for unrecognized values."""
        if raw is None:
            return "unknown"
        normalized = raw.lower().strip()
        return normalized if normalized in {"ok", "warning", "error", "info"} else "unknown"

    @classmethod
    def _parse_frontmatter(cls, text: str) -> tuple[dict | None, str]:
        """
        Extract the YAML frontmatter block (between leading --- delimiters).
        Returns (parsed_dict, body_text). Returns (None, text) when no valid frontmatter.

        Falls back to a line-based key:value parser for the known keys
        (status/summary/timestamp) when strict YAML parsing fails — tolerates
        common cases like `summary: [warning] text here` where the raw value
        is not valid YAML but is still human-readable.
        """
        stripped = text.lstrip("\n")
        if not stripped.startswith("---"):
            return None, text

        rest = stripped[3:]
        if rest.startswith("\n"):
            rest = rest[1:]

        fm_end = rest.find("\n---")
        if fm_end == -1:
            return None, text

        fm_text = rest[:fm_end]

        # Enforce max frontmatter size
        if len(fm_text.encode()) > cls.MAX_FRONTMATTER_BYTES:
            return None, text

        parsed: dict | None = None
        try:
            result = yaml.safe_load(fm_text)
            if isinstance(result, dict):
                parsed = result
        except Exception:
            parsed = None

        if parsed is None:
            parsed = cls._parse_frontmatter_lenient(fm_text)

        if parsed is None:
            return None, text

        body = rest[fm_end + 4:].lstrip("\n")
        return parsed, body

    @staticmethod
    def _parse_frontmatter_lenient(fm_text: str) -> dict | None:
        """
        Line-based fallback parser used when strict YAML fails.
        Extracts `status`, `summary`, `timestamp` from plain `key: value` lines.
        Returns None when none of the known keys are present.

        First occurrence of a key wins (opposite of YAML's last-wins semantics) —
        safer on malformed input, where a later garbled line would otherwise
        clobber a clean earlier value.
        """
        known_keys = {"status", "summary", "timestamp"}
        result: dict = {}
        for line in fm_text.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            if key not in known_keys or key in result:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
        return result or None

    @staticmethod
    def _extract_fallback_summary(body: str) -> str | None:
        """Return the first non-blank, non-heading line from the body (max 512 chars)."""
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:512]
        return None

    @staticmethod
    def _resolve_reported_at(
        frontmatter_ts: datetime | None,
        file_mtime: datetime | None,
    ) -> tuple[datetime | None, str | None]:
        """
        Resolve reported_at from available sources.
        Frontmatter timestamp wins; file mtime is the fallback.
        Returns (timestamp, source_string).
        """
        if frontmatter_ts is not None:
            return frontmatter_ts, "frontmatter"
        if file_mtime is not None:
            return file_mtime, "file_mtime"
        return None, None

    @classmethod
    async def _consume_download_stream(cls, stream: AsyncIterator[bytes]) -> str:
        """Read an async byte stream into a string, capped at MAX_RAW_BYTES."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > cls.MAX_RAW_BYTES:
                over = total - cls.MAX_RAW_BYTES
                safe_chunk = chunk[:-over] if over < len(chunk) else b""
                chunks.append(safe_chunk)
                return (
                    b"".join(chunks).decode("utf-8", errors="replace")
                    + "\n... (truncated)"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    @classmethod
    def _fire_status_updated_event(
        cls,
        environment: AgentEnvironment,
        severity: str | None,
        prev_severity: str | None,
        summary: str | None,
        reported_at: datetime | None,
        reported_at_source: str | None,
        fetched_at: datetime,
    ) -> None:
        """Emit agent_status_updated event via the event bus (best-effort; never raises)."""
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
                    event_type=EventType.AGENT_STATUS_UPDATED,
                    model_id=environment.agent_id,
                    user_id=owner_id,
                    meta={
                        "agent_id": str(environment.agent_id),
                        "environment_id": str(environment.id),
                        "severity": severity,
                        "prev_severity": prev_severity,
                        "summary": summary,
                        "reported_at": reported_at.isoformat() if reported_at else None,
                        "reported_at_source": reported_at_source,
                        "fetched_at": fetched_at.isoformat(),
                    },
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_emit())
            except RuntimeError:
                pass  # no running event loop in sync context
        except Exception as exc:
            logger.debug("Failed to emit agent_status_updated event: %s", exc)

    @classmethod
    def _create_transition_activity(
        cls,
        environment: AgentEnvironment,
        old_severity: str | None,
        new_severity: str,
    ) -> None:
        """Create an agent activity feed entry describing the severity transition."""
        from app.core.db import create_session
        from app.models.sessions.activity import ActivityCreate
        from app.services.events.activity_service import ActivityService
        from app.models.agents.agent import Agent as AgentModel

        with create_session() as sess:
            agent = sess.get(AgentModel, environment.agent_id)
            if not agent:
                return
            ActivityService.create_activity(
                db_session=sess,
                user_id=agent.owner_id,
                data=ActivityCreate(
                    agent_id=environment.agent_id,
                    activity_type="status_change",
                    text=f"Agent status changed: {old_severity or 'none'} → {new_severity}",
                ),
            )
