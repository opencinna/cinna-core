"""
Task Trigger Service - handles trigger CRUD, webhook validation, and trigger execution.
"""
import hmac
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID

import pytz
from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.core.db import engine
from app.core.security import encrypt_field, decrypt_field
from app.models import (
    InputTask,
    TaskTrigger,
    TriggerType,
    TaskTriggerCreateSchedule,
    TaskTriggerCreateExactDate,
    TaskTriggerCreateWebhook,
    TaskTriggerUpdate,
    InputTaskStatus,
    SessionCreate,
)
from app.services.agents.agent_scheduler_service import AgentSchedulerService
from app.services.ai_functions.ai_functions_service import AIFunctionsService

logger = logging.getLogger(__name__)


# ==================== Exception Classes ====================

class TriggerError(Exception):
    """Base exception for trigger service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TriggerNotFoundError(TriggerError):
    def __init__(self, message: str = "Trigger not found"):
        super().__init__(message, status_code=404)


class TriggerValidationError(TriggerError):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class TriggerPermissionError(TriggerError):
    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=403)


class WebhookTokenInvalidError(TriggerError):
    def __init__(self, message: str = "Invalid or expired token"):
        super().__init__(message, status_code=401)


# ==================== Service ====================

class TaskTriggerService:
    """Service for managing task triggers."""

    # ==================== Helper Methods ====================

    @staticmethod
    def verify_task_ownership(
        db_session: DBSession, task_id: UUID, user_id: UUID
    ) -> InputTask:
        """Get task and verify ownership."""
        task = db_session.get(InputTask, task_id)
        if not task:
            raise TriggerNotFoundError("Task not found")
        if task.owner_id != user_id:
            raise TriggerPermissionError()
        return task

    @staticmethod
    def get_trigger_with_check(
        db_session: DBSession, trigger_id: UUID, task_id: UUID, user_id: UUID
    ) -> TaskTrigger:
        """Get trigger, verify it belongs to the task and user owns the task."""
        TaskTriggerService.verify_task_ownership(db_session, task_id, user_id)
        trigger = db_session.get(TaskTrigger, trigger_id)
        if not trigger or trigger.task_id != task_id:
            raise TriggerNotFoundError()
        return trigger

    @staticmethod
    def generate_webhook_credentials() -> tuple[str, str, str, str]:
        """
        Generate webhook credentials.

        Returns:
            Tuple of (webhook_id, plaintext_token, encrypted_token, token_prefix)
        """
        webhook_id = secrets.token_urlsafe(8)  # ~11 char URL-safe slug
        token = secrets.token_urlsafe(32)  # ~43 char random token
        encrypted_token = encrypt_field(token)
        token_prefix = token[:8]
        return webhook_id, token, encrypted_token, token_prefix

    @staticmethod
    def _build_webhook_url(webhook_id: str) -> str:
        """Build the full public webhook URL."""
        base = settings.FRONTEND_HOST or "https://localhost"
        # Use backend API URL pattern
        return f"{base}/api/v1/hooks/{webhook_id}"

    # ==================== CRUD Methods ====================

    @staticmethod
    def create_schedule_trigger(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
        data: TaskTriggerCreateSchedule,
    ) -> TaskTrigger:
        """Create a schedule trigger using AI to parse natural language."""
        TaskTriggerService.verify_task_ownership(db_session, task_id, user_id)

        # Generate schedule from natural language (with per-user provider routing)
        from app.models.users.user import User
        user = db_session.get(User, user_id)
        result = AIFunctionsService.generate_schedule(
            data.natural_language, data.timezone, user=user, db=db_session
        )
        if not result.get("success"):
            raise TriggerValidationError(
                result.get("error", "Failed to parse schedule")
            )

        cron_local = result["cron_string"]
        description = result["description"]

        # Convert to UTC and calculate next execution
        cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
            cron_local, data.timezone
        )
        next_exec = AgentSchedulerService.calculate_next_execution(
            cron_utc, data.timezone
        )

        trigger = TaskTrigger(
            task_id=task_id,
            owner_id=user_id,
            type=TriggerType.SCHEDULE,
            name=data.name,
            payload_template=data.payload_template,
            cron_string=cron_utc,
            timezone=data.timezone,
            schedule_description=description,
            next_execution=next_exec,
        )
        db_session.add(trigger)
        db_session.commit()
        db_session.refresh(trigger)
        logger.info(f"Created schedule trigger {trigger.id} for task {task_id}")
        return trigger

    @staticmethod
    def create_exact_date_trigger(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
        data: TaskTriggerCreateExactDate,
    ) -> TaskTrigger:
        """Create a one-time exact date trigger."""
        TaskTriggerService.verify_task_ownership(db_session, task_id, user_id)

        # Convert execute_at to UTC if timezone provided
        execute_at_utc = data.execute_at
        if data.timezone:
            try:
                user_tz = pytz.timezone(data.timezone)
                if execute_at_utc.tzinfo is None:
                    # Naive datetime — localize to user timezone then convert to UTC
                    local_dt = user_tz.localize(execute_at_utc)
                    execute_at_utc = local_dt.astimezone(pytz.utc).replace(tzinfo=None)
                else:
                    execute_at_utc = execute_at_utc.astimezone(pytz.utc).replace(tzinfo=None)
            except Exception:
                pass  # Keep as-is if timezone conversion fails

        # Validate future date
        if execute_at_utc <= datetime.now(UTC):
            raise TriggerValidationError("Execution date must be in the future")

        trigger = TaskTrigger(
            task_id=task_id,
            owner_id=user_id,
            type=TriggerType.EXACT_DATE,
            name=data.name,
            payload_template=data.payload_template,
            execute_at=execute_at_utc,
            timezone=data.timezone,
            executed=False,
        )
        db_session.add(trigger)
        db_session.commit()
        db_session.refresh(trigger)
        logger.info(f"Created exact_date trigger {trigger.id} for task {task_id}")
        return trigger

    @staticmethod
    def create_webhook_trigger(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
        data: TaskTriggerCreateWebhook,
    ) -> tuple[TaskTrigger, str]:
        """
        Create a webhook trigger with generated credentials.

        Returns:
            Tuple of (trigger, plaintext_token)
        """
        TaskTriggerService.verify_task_ownership(db_session, task_id, user_id)

        webhook_id, token, encrypted_token, token_prefix = (
            TaskTriggerService.generate_webhook_credentials()
        )

        trigger = TaskTrigger(
            task_id=task_id,
            owner_id=user_id,
            type=TriggerType.WEBHOOK,
            name=data.name,
            payload_template=data.payload_template,
            webhook_id=webhook_id,
            webhook_token_encrypted=encrypted_token,
            webhook_token_prefix=token_prefix,
        )
        db_session.add(trigger)
        db_session.commit()
        db_session.refresh(trigger)
        logger.info(f"Created webhook trigger {trigger.id} for task {task_id}")
        return trigger, token

    @staticmethod
    def list_triggers(
        db_session: DBSession, task_id: UUID, user_id: UUID
    ) -> list[TaskTrigger]:
        """List all triggers for a task."""
        TaskTriggerService.verify_task_ownership(db_session, task_id, user_id)
        statement = (
            select(TaskTrigger)
            .where(TaskTrigger.task_id == task_id)
            .order_by(TaskTrigger.created_at)
        )
        return list(db_session.exec(statement).all())

    @staticmethod
    def get_trigger(
        db_session: DBSession, trigger_id: UUID, task_id: UUID, user_id: UUID
    ) -> TaskTrigger:
        """Get a single trigger with ownership check."""
        return TaskTriggerService.get_trigger_with_check(
            db_session, trigger_id, task_id, user_id
        )

    @staticmethod
    def update_trigger(
        db_session: DBSession,
        trigger_id: UUID,
        task_id: UUID,
        user_id: UUID,
        data: TaskTriggerUpdate,
    ) -> TaskTrigger:
        """Update a trigger. Re-runs AI schedule generation if schedule fields change."""
        trigger = TaskTriggerService.get_trigger_with_check(
            db_session, trigger_id, task_id, user_id
        )

        update_data = data.model_dump(exclude_unset=True)

        # Handle schedule updates with NL re-generation
        if trigger.type == TriggerType.SCHEDULE and data.natural_language:
            timezone = data.timezone or trigger.timezone or "UTC"
            from app.models.users.user import User
            user = db_session.get(User, user_id)
            result = AIFunctionsService.generate_schedule(
                data.natural_language, timezone, user=user, db=db_session
            )
            if not result.get("success"):
                raise TriggerValidationError(
                    result.get("error", "Failed to parse schedule")
                )

            cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                result["cron_string"], timezone
            )
            next_exec = AgentSchedulerService.calculate_next_execution(
                cron_utc, timezone
            )

            trigger.cron_string = cron_utc
            trigger.timezone = timezone
            trigger.schedule_description = result["description"]
            trigger.next_execution = next_exec
            # Remove from update_data to avoid double-setting
            update_data.pop("natural_language", None)
            update_data.pop("timezone", None)

        # Handle exact_date execute_at update
        if trigger.type == TriggerType.EXACT_DATE and data.execute_at is not None:
            execute_at_utc = data.execute_at
            tz = data.timezone or trigger.timezone
            if tz:
                try:
                    user_tz = pytz.timezone(tz)
                    if execute_at_utc.tzinfo is None:
                        local_dt = user_tz.localize(execute_at_utc)
                        execute_at_utc = local_dt.astimezone(pytz.utc).replace(tzinfo=None)
                    else:
                        execute_at_utc = execute_at_utc.astimezone(pytz.utc).replace(tzinfo=None)
                except Exception:
                    pass
            if execute_at_utc <= datetime.now(UTC):
                raise TriggerValidationError("Execution date must be in the future")
            trigger.execute_at = execute_at_utc
            trigger.executed = False
            update_data.pop("execute_at", None)

        # Apply remaining simple field updates
        skip_fields = {"natural_language", "timezone", "execute_at"}
        for key, value in update_data.items():
            if key not in skip_fields and hasattr(trigger, key):
                setattr(trigger, key, value)

        # Update timezone if provided and not already handled by schedule re-gen
        if "timezone" in update_data and trigger.type != TriggerType.SCHEDULE:
            trigger.timezone = update_data["timezone"]

        trigger.updated_at = datetime.now(UTC)
        db_session.commit()
        db_session.refresh(trigger)
        logger.info(f"Updated trigger {trigger_id}")
        return trigger

    @staticmethod
    def delete_trigger(
        db_session: DBSession, trigger_id: UUID, task_id: UUID, user_id: UUID
    ) -> bool:
        """Delete a trigger."""
        trigger = TaskTriggerService.get_trigger_with_check(
            db_session, trigger_id, task_id, user_id
        )
        db_session.delete(trigger)
        db_session.commit()
        logger.info(f"Deleted trigger {trigger_id}")
        return True

    @staticmethod
    def regenerate_webhook_token(
        db_session: DBSession, trigger_id: UUID, task_id: UUID, user_id: UUID
    ) -> tuple[TaskTrigger, str]:
        """Regenerate webhook token. Only for webhook triggers."""
        trigger = TaskTriggerService.get_trigger_with_check(
            db_session, trigger_id, task_id, user_id
        )
        if trigger.type != TriggerType.WEBHOOK:
            raise TriggerValidationError("Token regeneration is only for webhook triggers")

        token = secrets.token_urlsafe(32)
        trigger.webhook_token_encrypted = encrypt_field(token)
        trigger.webhook_token_prefix = token[:8]
        trigger.updated_at = datetime.now(UTC)
        db_session.commit()
        db_session.refresh(trigger)
        logger.info(f"Regenerated token for webhook trigger {trigger_id}")
        return trigger, token

    # ==================== Webhook Execution ====================

    @staticmethod
    def validate_webhook_token(
        db_session: DBSession, webhook_id: str, provided_token: str
    ) -> TaskTrigger:
        """Validate webhook token and return the trigger if valid."""
        statement = select(TaskTrigger).where(
            TaskTrigger.webhook_id == webhook_id,
            TaskTrigger.type == TriggerType.WEBHOOK,
        )
        trigger = db_session.exec(statement).first()

        # Return 404 for disabled or non-existent triggers (no information leakage)
        if not trigger or not trigger.enabled:
            raise TriggerNotFoundError("Webhook not found")

        # Decrypt stored token and compare (timing-safe)
        stored_token = decrypt_field(trigger.webhook_token_encrypted)
        if not hmac.compare_digest(stored_token, provided_token):
            raise WebhookTokenInvalidError()

        return trigger

    @staticmethod
    async def fire_trigger(
        db_session: DBSession,
        trigger: TaskTrigger,
        payload: str | None = None,
    ) -> None:
        """
        Fire a trigger: assemble prompt and execute the task.

        Args:
            db_session: Database session
            trigger: The trigger to fire
            payload: Dynamic payload (webhook body)
        """
        from app.services.tasks.input_task_service import InputTaskService

        task = db_session.get(InputTask, trigger.task_id)
        if not task:
            logger.error(f"Trigger {trigger.id}: task {trigger.task_id} not found")
            return

        if not task.selected_agent_id:
            logger.error(
                f"Trigger {trigger.id}: task {trigger.task_id} has no selected agent"
            )
            return

        # Assemble prompt
        parts = [task.current_description]
        if trigger.payload_template or payload:
            parts.append("---")
            parts.append(f"Trigger: {trigger.name}")
            if trigger.payload_template:
                parts.append(trigger.payload_template)
            if payload:
                parts.append(payload)

        combined_prompt = "\n\n".join(parts)

        # Execute task
        success, session, error = await InputTaskService.execute_task(
            db_session=db_session,
            task=task,
            user_id=trigger.owner_id,
            message_to_send=combined_prompt,
        )

        if not success:
            logger.error(
                f"Trigger {trigger.id} fire failed for task {trigger.task_id}: {error}"
            )
        else:
            logger.info(
                f"Trigger {trigger.id} fired for task {trigger.task_id}, "
                f"session={session.id if session else 'None'}"
            )

        # Update trigger execution info
        trigger.last_execution = datetime.now(UTC)
        if trigger.type == TriggerType.SCHEDULE:
            try:
                trigger.next_execution = AgentSchedulerService.calculate_next_execution(
                    trigger.cron_string, trigger.timezone or "UTC"
                )
            except Exception as e:
                logger.error(f"Failed to recalculate next_execution: {e}")
        elif trigger.type == TriggerType.EXACT_DATE:
            trigger.executed = True

        trigger.updated_at = datetime.now(UTC)
        db_session.commit()

    # ==================== Scheduler Polling ====================

    @staticmethod
    async def poll_due_triggers() -> None:
        """Poll for due schedule and exact-date triggers and fire them."""
        import asyncio

        now = datetime.now(UTC)

        with DBSession(engine) as db_session:
            # Query due schedule triggers
            schedule_stmt = select(TaskTrigger).where(
                TaskTrigger.type == TriggerType.SCHEDULE,
                TaskTrigger.enabled == True,  # noqa: E712
                TaskTrigger.next_execution <= now,
            )
            schedule_triggers = list(db_session.exec(schedule_stmt).all())

            # Query due exact-date triggers
            exact_date_stmt = select(TaskTrigger).where(
                TaskTrigger.type == TriggerType.EXACT_DATE,
                TaskTrigger.enabled == True,  # noqa: E712
                TaskTrigger.executed == False,  # noqa: E712
                TaskTrigger.execute_at <= now,
            )
            exact_date_triggers = list(db_session.exec(exact_date_stmt).all())

            all_due = schedule_triggers + exact_date_triggers
            if not all_due:
                return

            logger.info(
                f"Found {len(schedule_triggers)} schedule + "
                f"{len(exact_date_triggers)} exact-date triggers due"
            )

            for trigger in all_due:
                try:
                    await TaskTriggerService.fire_trigger(db_session, trigger)
                except Exception as e:
                    logger.error(
                        f"Error firing trigger {trigger.id}: {e}", exc_info=True
                    )
