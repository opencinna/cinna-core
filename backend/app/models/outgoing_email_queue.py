"""
OutgoingEmailQueue model - Queued agent replies to be sent via SMTP.

Each entry represents an agent response that needs to be sent back
as an email via the parent agent's SMTP configuration.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class OutgoingEmailStatus:
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class OutgoingEmailQueueBase(SQLModel):
    """Shared fields for outgoing email queue."""
    recipient: str = Field(max_length=320)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(default="", sa_column=Column(Text))
    references: str | None = Field(default=None, sa_column=Column(Text))
    in_reply_to: str | None = Field(default=None, max_length=512)


class OutgoingEmailQueue(OutgoingEmailQueueBase, table=True):
    """Database table for outgoing email queue."""
    __tablename__ = "outgoing_email_queue"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    clone_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agent.id", ondelete="CASCADE")
    session_id: uuid.UUID | None = Field(default=None, foreign_key="session.id", ondelete="CASCADE")
    message_id: uuid.UUID | None = Field(default=None, foreign_key="message.id", ondelete="CASCADE")
    input_task_id: uuid.UUID | None = Field(default=None, foreign_key="input_task.id", ondelete="SET NULL")

    status: str = Field(default=OutgoingEmailStatus.PENDING)
    retry_count: int = Field(default=0)
    last_error: str | None = Field(default=None, sa_column=Column(Text))

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sent_at: datetime | None = Field(default=None)


class OutgoingEmailQueuePublic(OutgoingEmailQueueBase):
    """Public representation of an outgoing email queue entry."""
    id: uuid.UUID
    agent_id: uuid.UUID
    clone_agent_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    input_task_id: uuid.UUID | None = None
    status: str
    retry_count: int
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None = None
