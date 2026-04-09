"""
Agent Scheduler Service - handles all scheduler-related business logic.

This service:
- Calculates next execution time from CRON strings
- Creates, reads, updates, and deletes AgentSchedule records
- Manages multi-schedule CRUD operations per agent
- Enforces agent ownership and schedule access control
"""
import logging
from datetime import UTC, datetime, timedelta
import uuid
import pytz
from croniter import croniter
from sqlmodel import Session, select

from app.models import Agent, AgentSchedule

logger = logging.getLogger(__name__)


# ==================== Domain Exceptions ====================


class ScheduleError(Exception):
    """Base exception for agent schedule service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScheduleNotFoundError(ScheduleError):
    """Schedule not found."""

    def __init__(self, message: str = "Schedule not found for this agent"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(ScheduleError):
    """Agent not found."""

    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class PermissionDeniedError(ScheduleError):
    """Permission denied."""

    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=400)


class InvalidCronError(ScheduleError):
    """Invalid CRON expression or timezone."""

    def __init__(self, detail: str):
        super().__init__(f"Invalid CRON string or timezone: {detail}", status_code=400)


# ==================== Service ====================


class AgentSchedulerService:
    """Service for managing agent schedules."""

    # ==================== Access Control Helpers ====================

    @staticmethod
    def verify_agent_access(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> Agent:
        """
        Verify agent exists and user has access.

        Args:
            session: Database session
            agent_id: Agent UUID to verify
            user_id: User ID requesting access
            is_superuser: Whether the user is a superuser (bypasses ownership check)

        Returns:
            Agent instance if valid

        Raises:
            AgentNotFoundError: If agent doesn't exist
            PermissionDeniedError: If user doesn't own the agent
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentNotFoundError()
        if not is_superuser and agent.owner_id != user_id:
            raise PermissionDeniedError()
        return agent

    @staticmethod
    def get_schedule_for_agent(
        session: Session,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
    ) -> AgentSchedule:
        """
        Get a schedule and verify it belongs to the given agent.

        Args:
            session: Database session
            agent_id: Agent UUID the schedule should belong to
            schedule_id: Schedule UUID to fetch

        Returns:
            AgentSchedule instance

        Raises:
            ScheduleNotFoundError: If schedule doesn't exist or doesn't belong to the agent
        """
        schedule = session.get(AgentSchedule, schedule_id)
        if not schedule or schedule.agent_id != agent_id:
            raise ScheduleNotFoundError()
        return schedule

    # ==================== CRON Utilities ====================

    @staticmethod
    def convert_local_cron_to_utc(cron_string: str, timezone: str) -> str:
        """
        Convert CRON expression from local time to UTC.

        Args:
            cron_string: CRON expression in local time
            timezone: User's IANA timezone

        Returns:
            CRON expression in UTC

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
        """
        try:
            user_tz = pytz.timezone(timezone)

            # Parse the cron string
            parts = cron_string.split()
            if len(parts) != 5:
                raise ValueError("Invalid CRON format")

            minute, hour, day, month, day_of_week = parts

            # If hour is *, don't convert (hourly schedules)
            if hour == '*' or '/' in hour:
                return cron_string

            # Create a naive datetime (we'll use tomorrow to avoid edge cases with current time)
            # Must be naive (no tzinfo) so that pytz.localize() can attach the user timezone.
            naive_dt = datetime.now(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            ) + timedelta(days=1)

            # Handle hour ranges (e.g., "9-17")
            if '-' in hour:
                start, end = hour.split('-')
                start_hour = int(start)
                end_hour = int(end)

                # Create localized datetime in user timezone
                local_dt_start = user_tz.localize(naive_dt.replace(hour=start_hour))
                local_dt_end = user_tz.localize(naive_dt.replace(hour=end_hour))

                # Convert to UTC
                utc_dt_start = local_dt_start.astimezone(pytz.utc)
                utc_dt_end = local_dt_end.astimezone(pytz.utc)

                utc_hour = f"{utc_dt_start.hour}-{utc_dt_end.hour}"
            else:
                # Single hour or comma-separated hours
                if ',' in hour:
                    hours = [int(h) for h in hour.split(',')]
                else:
                    hours = [int(hour)]

                utc_hours = []
                for h in hours:
                    # Create localized datetime in user timezone
                    local_dt = user_tz.localize(naive_dt.replace(hour=h))

                    # Convert to UTC
                    utc_dt = local_dt.astimezone(pytz.utc)
                    utc_hours.append(utc_dt.hour)

                utc_hour = ','.join(str(h) for h in utc_hours) if len(utc_hours) > 1 else str(utc_hours[0])

            result = f"{minute} {utc_hour} {day} {month} {day_of_week}"
            return result
        except InvalidCronError:
            raise
        except Exception as e:
            logger.error(f"Failed to convert CRON to UTC: {e}", exc_info=True)
            raise InvalidCronError(str(e))

    @staticmethod
    def calculate_next_execution(cron_string: str) -> datetime:
        """
        Calculate next execution time from CRON string.

        Args:
            cron_string: CRON expression in UTC

        Returns:
            Next execution datetime in UTC

        Raises:
            InvalidCronError: If CRON string is invalid
        """
        try:
            # Create croniter with current UTC time
            now_utc = datetime.now(pytz.utc)
            cron = croniter(cron_string, now_utc)

            # Get next run as datetime
            next_run = cron.get_next(datetime)

            # Check if it's naive or aware
            if next_run.tzinfo is None:
                # Naive datetime - assume it's UTC and localize
                next_run_utc = pytz.utc.localize(next_run)
            else:
                # Already aware - ensure it's in UTC
                next_run_utc = next_run.astimezone(pytz.utc)
            return next_run_utc
        except InvalidCronError:
            raise
        except Exception as e:
            logger.error(f"Failed to calculate next execution: {e}", exc_info=True)
            raise InvalidCronError(str(e))

    # ==================== Generate Preview ====================

    @staticmethod
    def generate_schedule_preview(
        natural_language: str,
        timezone: str,
        user: "User | None" = None,
        db: "Session | None" = None,
    ) -> dict:
        """
        Generate a CRON schedule from natural language and calculate next execution.

        Orchestrates the AI call, CRON conversion, and next_execution calculation.

        Args:
            natural_language: User's schedule description
            timezone: User's IANA timezone

        Returns:
            Dict with success, cron_string, description, next_execution, or error
        """
        from app.services.ai_functions.ai_functions_service import AIFunctionsService

        ai_result = AIFunctionsService.generate_schedule(
            natural_language=natural_language,
            timezone=timezone,
            user=user,
            db=db,
        )

        if ai_result.get("success"):
            try:
                cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                    ai_result["cron_string"], timezone
                )
                next_exec = AgentSchedulerService.calculate_next_execution(cron_utc)
                ai_result["next_execution"] = next_exec.isoformat()
            except (InvalidCronError, Exception) as e:
                return {
                    "success": False,
                    "error": f"Failed to calculate next execution: {str(e)}",
                }

        return ai_result

    # ==================== CRUD Operations ====================

    @staticmethod
    def create_schedule(
        *,
        session: Session,
        agent_id: uuid.UUID,
        name: str,
        cron_string: str,
        timezone: str,
        description: str,
        prompt: str | None = None,
        enabled: bool = True,
        schedule_type: str = "static_prompt",
        command: str | None = None,
    ) -> AgentSchedule:
        """
        Create a new agent schedule.

        Args:
            session: Database session
            agent_id: Agent UUID
            name: User-friendly label
            cron_string: CRON expression in local time
            timezone: User's IANA timezone (used transiently for conversion, not stored)
            description: Human-readable description
            prompt: Schedule-specific prompt (None = use agent's entrypoint_prompt)
            enabled: Whether schedule is enabled
            schedule_type: "static_prompt" (default) or "script_trigger"
            command: Shell command for script_trigger type (None for static_prompt)

        Returns:
            Created AgentSchedule

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
        """
        # Convert CRON from local time to UTC
        cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
            cron_string, timezone
        )

        # Calculate next execution
        next_exec = AgentSchedulerService.calculate_next_execution(cron_utc)

        schedule = AgentSchedule(
            agent_id=agent_id,
            name=name,
            cron_string=cron_utc,
            description=description,
            prompt=prompt,
            enabled=enabled,
            next_execution=next_exec,
            schedule_type=schedule_type,
            command=command,
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(f"Created schedule '{name}' (type={schedule_type}) for agent {agent_id}: {description}")
        return schedule

    @staticmethod
    def get_agent_schedules(
        session: Session,
        agent_id: uuid.UUID,
    ) -> list[AgentSchedule]:
        """
        Get all schedules for an agent, ordered by created_at.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            List of AgentSchedule records
        """
        statement = (
            select(AgentSchedule)
            .where(AgentSchedule.agent_id == agent_id)
            .order_by(AgentSchedule.created_at)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def update_schedule(
        session: Session,
        schedule: AgentSchedule,
        **fields,
    ) -> AgentSchedule:
        """
        Partial update of an agent schedule.

        If cron_string changes, timezone must be provided for UTC conversion
        and next_execution is recalculated.

        Args:
            session: Database session
            schedule: AgentSchedule instance (already verified)
            **fields: Fields to update (name, cron_string, timezone, description, prompt, enabled)

        Returns:
            Updated AgentSchedule

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
            ScheduleError: If timezone missing when updating cron_string
        """
        # Handle cron_string change (requires timezone for conversion)
        if "cron_string" in fields and fields["cron_string"] is not None:
            timezone = fields.pop("timezone", None)
            if not timezone:
                raise ScheduleError("timezone is required when updating cron_string")

            cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                fields["cron_string"], timezone
            )
            schedule.cron_string = cron_utc
            schedule.next_execution = AgentSchedulerService.calculate_next_execution(cron_utc)
            del fields["cron_string"]
        else:
            # Remove timezone if present but cron_string not changing
            fields.pop("timezone", None)

        # Apply remaining field updates
        for key, value in fields.items():
            if value is not None or key == "prompt":  # Allow setting prompt to None
                setattr(schedule, key, value)

        schedule.updated_at = datetime.now(UTC)
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(f"Updated schedule {schedule.id}")
        return schedule

    @staticmethod
    def delete_schedule(
        session: Session,
        schedule: AgentSchedule,
    ) -> None:
        """
        Delete a schedule.

        Args:
            session: Database session
            schedule: AgentSchedule instance (already verified)
        """
        schedule_id = schedule.id
        session.delete(schedule)
        session.commit()
        logger.info(f"Deleted schedule {schedule_id}")

    @staticmethod
    def get_all_enabled_schedules(session: Session) -> list[AgentSchedule]:
        """
        Get all enabled schedules.

        Args:
            session: Database session

        Returns:
            List of enabled AgentSchedule records
        """
        statement = select(AgentSchedule).where(AgentSchedule.enabled == True)  # noqa: E712
        return list(session.exec(statement).all())

    @staticmethod
    def update_execution_time(
        session: Session,
        schedule_id: uuid.UUID,
        last_execution: datetime,
    ) -> None:
        """
        Update schedule execution times after running an agent.

        Args:
            session: Database session
            schedule_id: Schedule UUID
            last_execution: When the agent was executed
        """
        schedule = session.get(AgentSchedule, schedule_id)
        if schedule:
            schedule.last_execution = last_execution
            schedule.next_execution = AgentSchedulerService.calculate_next_execution(
                schedule.cron_string
            )
            schedule.updated_at = datetime.now(UTC)
            session.commit()
            logger.info(
                f"Updated execution time for schedule {schedule_id}. "
                f"Next run: {schedule.next_execution}"
            )

    # ==================== Schedule Log Operations ====================

    @staticmethod
    def create_log(
        session: Session,
        *,
        schedule_id: uuid.UUID,
        agent_id: uuid.UUID,
        schedule_type: str,
        status: str,
        prompt_used: str | None = None,
        command_executed: str | None = None,
        command_output: str | None = None,
        command_exit_code: int | None = None,
        session_id: uuid.UUID | None = None,
        error_message: str | None = None,
    ) -> "AgentScheduleLog":
        """
        Create an immutable execution log entry for a schedule run.

        Args:
            session: Database session
            schedule_id: Schedule that was executed
            agent_id: Agent the schedule belongs to
            schedule_type: Type snapshot at execution time
            status: "success", "session_triggered", or "error"
            prompt_used: Prompt sent (static_prompt only)
            command_executed: Command that ran (script_trigger only)
            command_output: stdout from command (script_trigger only)
            command_exit_code: Exit code from command (script_trigger only)
            session_id: Session created (if any)
            error_message: Error details if status is "error"

        Returns:
            Created AgentScheduleLog
        """
        from app.models import AgentScheduleLog
        log = AgentScheduleLog(
            schedule_id=schedule_id,
            agent_id=agent_id,
            schedule_type=schedule_type,
            status=status,
            prompt_used=prompt_used,
            command_executed=command_executed,
            command_output=command_output,
            command_exit_code=command_exit_code,
            session_id=session_id,
            error_message=error_message,
            executed_at=datetime.now(UTC),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        logger.debug(
            f"Created schedule log for schedule {schedule_id}: "
            f"type={schedule_type}, status={status}"
        )
        return log

    @staticmethod
    def get_schedule_logs(
        session: Session,
        schedule_id: uuid.UUID,
        limit: int = 50,
    ) -> list["AgentScheduleLog"]:
        """
        Get recent execution logs for a schedule, ordered by executed_at DESC.

        Args:
            session: Database session
            schedule_id: Schedule UUID to query
            limit: Maximum number of logs to return (default 50)

        Returns:
            List of AgentScheduleLog records, newest first
        """
        from app.models import AgentScheduleLog
        from sqlmodel import desc
        statement = (
            select(AgentScheduleLog)
            .where(AgentScheduleLog.schedule_id == schedule_id)
            .order_by(desc(AgentScheduleLog.executed_at))
            .limit(limit)
        )
        return list(session.exec(statement).all())

    # ==================== Manual Execution ====================

    @staticmethod
    async def execute_now(session: Session, agent_id: uuid.UUID, schedule_id: uuid.UUID) -> None:
        """
        Execute a schedule immediately, identical to cron execution.

        Args:
            session: Database session
            agent_id: Agent UUID
            schedule_id: Schedule UUID (must belong to agent, already verified by caller)

        Raises:
            ScheduleError: If agent is inactive or execution fails
        """
        from app.services.sessions.session_service import SessionService
        from app.services.environments.agent_env_connector import agent_env_connector
        from app.services.events.activity_service import ActivityService
        from app.services.agents.agent_schedule_scheduler import (
            _execute_static_prompt,
            _execute_script_trigger,
        )

        agent = session.get(Agent, agent_id)
        if not agent or not agent.is_active:
            raise ScheduleError("Agent is not active", status_code=400)

        schedule = session.get(AgentSchedule, schedule_id)

        try:
            if schedule.schedule_type == "script_trigger":
                await _execute_script_trigger(
                    schedule=schedule,
                    agent=agent,
                    db_session=session,
                    session_service=SessionService,
                    activity_service=ActivityService,
                    env_connector=agent_env_connector,
                )
            else:
                await _execute_static_prompt(
                    schedule=schedule,
                    agent=agent,
                    db_session=session,
                    session_service=SessionService,
                )
        except Exception as e:
            logger.error(f"Manual schedule execution failed for {schedule_id}: {e}", exc_info=True)
            raise ScheduleError(f"Schedule execution failed: {e}", status_code=500)

    # ==================== Environment Helpers ====================

    @staticmethod
    def get_active_environment(
        session: Session,
        agent_id: uuid.UUID,
    ) -> "AgentEnvironment | None":
        """
        Returns the agent's active environment, or None if not configured.

        Args:
            session: Database session
            agent_id: Agent UUID to look up

        Returns:
            AgentEnvironment if found and set as active, otherwise None
        """
        from app.models import AgentEnvironment
        agent = session.get(Agent, agent_id)
        if not agent or not agent.active_environment_id:
            return None
        return session.get(AgentEnvironment, agent.active_environment_id)

    @staticmethod
    async def ensure_environment_running(
        environment: "AgentEnvironment",
        get_fresh_db_session: "callable",
    ) -> "AgentEnvironment":
        """
        Activate environment if suspended/stopped. Returns running environment or raises.

        Reuses activation patterns from SessionService — suspended → activate,
        stopped → start. Polls every 5 seconds up to 120 seconds for running status.

        Args:
            environment: AgentEnvironment to activate
            get_fresh_db_session: Callable returning a DB session context manager

        Returns:
            Running AgentEnvironment (refreshed from DB)

        Raises:
            RuntimeError: If environment is in error state, unknown state, or times out
        """
        import asyncio
        from app.models import AgentEnvironment
        from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager

        status = environment.status
        env_id = environment.id

        if status == "running":
            return environment

        if status == "error":
            raise RuntimeError(
                f"Environment {env_id} is in error state and cannot be activated"
            )

        lifecycle = EnvironmentLifecycleManager()

        if status == "suspended":
            logger.info(f"Schedule exec: activating suspended environment {env_id}")
            await lifecycle.activate_suspended_environment(str(env_id))
        elif status == "stopped":
            logger.info(f"Schedule exec: starting stopped environment {env_id}")
            await lifecycle.start_environment(str(env_id))
        elif status in ("activating", "starting"):
            # Another process has already triggered activation — just poll
            logger.info(
                f"Schedule exec: environment {env_id} is already {status}, polling..."
            )
        else:
            raise RuntimeError(
                f"Environment {env_id} is in unexpected state '{status}' — cannot proceed"
            )

        # Poll until running or timeout (120 seconds)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 120
        while loop.time() < deadline:
            await asyncio.sleep(5)
            with get_fresh_db_session() as fresh_session:
                fresh_env = fresh_session.get(AgentEnvironment, env_id)
                if not fresh_env:
                    raise RuntimeError(
                        f"Environment {env_id} disappeared during activation"
                    )
                if fresh_env.status == "running":
                    logger.info(f"Schedule exec: environment {env_id} is now running")
                    return fresh_env
                if fresh_env.status == "error":
                    raise RuntimeError(
                        f"Environment {env_id} entered error state during activation"
                    )
                logger.debug(
                    f"Schedule exec: environment {env_id} status={fresh_env.status}, continuing to poll"
                )

        raise RuntimeError(
            f"Environment {env_id} activation timed out after 120 seconds"
        )
