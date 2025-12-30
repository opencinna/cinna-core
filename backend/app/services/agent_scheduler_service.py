"""
Agent Scheduler Service - handles all scheduler-related business logic.

This service:
- Calculates next execution time from CRON strings
- Creates and updates AgentSchedule records
- Manages schedule CRUD operations
"""
import logging
from datetime import datetime
import uuid
import pytz
from croniter import croniter
from sqlmodel import Session, select

from app.models import AgentSchedule

logger = logging.getLogger(__name__)


class AgentSchedulerService:
    """Service for managing agent schedules."""

    @staticmethod
    def convert_local_cron_to_utc(cron_string: str, timezone: str) -> str:
        """
        Convert CRON expression from local time to UTC.

        Args:
            cron_string: CRON expression in local time
            timezone: User's IANA timezone

        Returns:
            CRON expression in UTC
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
            naive_dt = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            # Add one day to avoid any "now" edge cases
            from datetime import timedelta
            naive_dt = naive_dt + timedelta(days=1)

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
        except Exception as e:
            logger.error(f"Failed to convert CRON to UTC: {e}", exc_info=True)
            raise ValueError(f"Invalid CRON string or timezone: {str(e)}")

    @staticmethod
    def calculate_next_execution(cron_string: str, timezone: str) -> datetime:
        """
        Calculate next execution time from CRON string.

        Args:
            cron_string: CRON expression in UTC
            timezone: User's IANA timezone (not used, kept for compatibility)

        Returns:
            Next execution datetime in UTC
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
        except Exception as e:
            logger.error(f"Failed to calculate next execution: {e}", exc_info=True)
            raise ValueError(f"Invalid CRON string or timezone: {str(e)}")

    @staticmethod
    def create_or_update_schedule(
        *,
        session: Session,
        agent_id: uuid.UUID,
        cron_string: str,
        timezone: str,
        description: str,
        enabled: bool = True
    ) -> AgentSchedule:
        """
        Create or update agent schedule.

        For now, we only support one schedule per agent, so this will
        update the existing one or create new one.

        Args:
            session: Database session
            agent_id: Agent UUID
            cron_string: CRON expression in local time
            timezone: User's IANA timezone
            description: Human-readable description
            enabled: Whether schedule is enabled

        Returns:
            Created or updated AgentSchedule

        Raises:
            ValueError: If CRON string or timezone is invalid
        """
        try:

            # Convert CRON from local time to UTC
            cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                cron_string, timezone
            )

            # Calculate next execution
            next_exec = AgentSchedulerService.calculate_next_execution(
                cron_utc, timezone
            )

            # Check if schedule exists
            statement = select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
            existing = session.exec(statement).first()

            if existing:
                # Update existing
                existing.cron_string = cron_utc
                existing.timezone = timezone
                existing.description = description
                existing.enabled = enabled
                existing.next_execution = next_exec
                existing.updated_at = datetime.utcnow()
                schedule = existing
                logger.info(f"Updated schedule for agent {agent_id}: {description}")
            else:
                # Create new
                schedule = AgentSchedule(
                    agent_id=agent_id,
                    cron_string=cron_utc,
                    timezone=timezone,
                    description=description,
                    enabled=enabled,
                    next_execution=next_exec
                )
                session.add(schedule)
                logger.info(f"Created schedule for agent {agent_id}: {description}")

            session.commit()
            session.refresh(schedule)
            return schedule
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create/update schedule: {e}", exc_info=True)
            raise

    @staticmethod
    def get_agent_schedule(
        session: Session,
        agent_id: uuid.UUID
    ) -> AgentSchedule | None:
        """
        Get active schedule for an agent.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            AgentSchedule if exists, None otherwise
        """
        statement = select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
        return session.exec(statement).first()

    @staticmethod
    def delete_schedule(
        session: Session,
        agent_id: uuid.UUID
    ) -> bool:
        """
        Delete agent schedule.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            True if schedule was deleted, False if not found
        """
        try:
            schedule = AgentSchedulerService.get_agent_schedule(session, agent_id)
            if schedule:
                session.delete(schedule)
                session.commit()
                logger.info(f"Deleted schedule for agent {agent_id}")
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to delete schedule: {e}", exc_info=True)
            raise

    @staticmethod
    def get_all_enabled_schedules(session: Session) -> list[AgentSchedule]:
        """
        Get all enabled schedules.

        This is useful for the future schedule runner that will
        execute agents based on their schedules.

        Args:
            session: Database session

        Returns:
            List of enabled AgentSchedule records
        """
        statement = select(AgentSchedule).where(AgentSchedule.enabled == True)
        return list(session.exec(statement).all())

    @staticmethod
    def update_execution_time(
        session: Session,
        schedule_id: uuid.UUID,
        last_execution: datetime
    ) -> None:
        """
        Update schedule execution times after running an agent.

        This is for future use by the schedule runner.

        Args:
            session: Database session
            schedule_id: Schedule UUID
            last_execution: When the agent was executed
        """
        try:
            schedule = session.get(AgentSchedule, schedule_id)
            if schedule:
                schedule.last_execution = last_execution
                schedule.next_execution = AgentSchedulerService.calculate_next_execution(
                    schedule.cron_string,
                    schedule.timezone
                )
                schedule.updated_at = datetime.utcnow()
                session.commit()
                logger.info(
                    f"Updated execution time for schedule {schedule_id}. "
                    f"Next run: {schedule.next_execution}"
                )
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to update execution time: {e}", exc_info=True)
            raise
