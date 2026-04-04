"""
Task Trigger models for automatic and event-driven task execution.

Supports three trigger types:
- Schedule: Recurring CRON-based execution
- Exact Date: One-time execution at a specific datetime
- Webhook: External HTTP-triggered execution with token validation
"""
import uuid
from datetime import datetime, UTC
from typing import Literal

from pydantic import computed_field
from sqlmodel import Field, SQLModel, Index

from app.core.config import settings


class TriggerType:
    """Trigger type constants."""
    SCHEDULE = "schedule"
    EXACT_DATE = "exact_date"
    WEBHOOK = "webhook"


# Database model
class TaskTrigger(SQLModel, table=True):
    __tablename__ = "task_trigger"
    __table_args__ = (
        Index("ix_task_trigger_task_id", "task_id"),
        Index("ix_task_trigger_schedule_poll", "type", "enabled", "next_execution"),
        Index("ix_task_trigger_exact_date_poll", "type", "enabled", "execute_at", "executed"),
        Index("ix_task_trigger_webhook_id", "webhook_id", unique=True),
        Index("ix_task_trigger_owner_id", "owner_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="input_task.id", nullable=False, ondelete="CASCADE")
    owner_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")
    type: str = Field(nullable=False)  # "schedule" | "exact_date" | "webhook"
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = Field(default=True)
    payload_template: str | None = Field(default=None, max_length=10000)

    # Schedule-specific fields (type="schedule")
    cron_string: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    schedule_description: str | None = Field(default=None)
    last_execution: datetime | None = Field(default=None)
    next_execution: datetime | None = Field(default=None)

    # Exact date fields (type="exact_date")
    execute_at: datetime | None = Field(default=None)
    executed: bool = Field(default=False)

    # Webhook fields (type="webhook")
    webhook_token_encrypted: str | None = Field(default=None)
    webhook_token_prefix: str | None = Field(default=None)
    webhook_id: str | None = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Create schemas
class TaskTriggerCreateSchedule(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["schedule"] = "schedule"
    payload_template: str | None = Field(default=None, max_length=10000)
    natural_language: str = Field(min_length=1, max_length=500)
    timezone: str = Field(min_length=1, max_length=100)


class TaskTriggerCreateExactDate(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["exact_date"] = "exact_date"
    payload_template: str | None = Field(default=None, max_length=10000)
    execute_at: datetime
    timezone: str = Field(min_length=1, max_length=100)


class TaskTriggerCreateWebhook(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["webhook"] = "webhook"
    payload_template: str | None = Field(default=None, max_length=10000)


# Update schema
class TaskTriggerUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    payload_template: str | None = Field(default=None, max_length=10000)
    # Schedule updates
    natural_language: str | None = Field(default=None, min_length=1, max_length=500)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    # Exact date updates
    execute_at: datetime | None = None


# Public response schemas
class TaskTriggerPublic(SQLModel):
    id: uuid.UUID
    task_id: uuid.UUID
    type: str
    name: str
    enabled: bool
    payload_template: str | None

    # Schedule fields
    cron_string: str | None = None
    timezone: str | None = None
    schedule_description: str | None = None
    last_execution: datetime | None = None
    next_execution: datetime | None = None

    # Exact date fields
    execute_at: datetime | None = None
    executed: bool = False

    # Webhook fields
    webhook_id: str | None = None
    webhook_token_prefix: str | None = None
    webhook_url: str | None = None

    # Timestamps
    created_at: datetime
    updated_at: datetime


class TaskTriggerPublicWithToken(TaskTriggerPublic):
    """Returned only on webhook creation/regeneration — includes full plaintext token."""
    webhook_token: str | None = None


class TaskTriggersPublic(SQLModel):
    data: list[TaskTriggerPublic]
    count: int
