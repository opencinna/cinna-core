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
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(f"Created schedule '{name}' for agent {agent_id}: {description}")
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
