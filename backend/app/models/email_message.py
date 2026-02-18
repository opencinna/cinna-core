"""
EmailMessage model - Stores parsed incoming emails for processing.

Each email is stored when polled from IMAP, then routed to the correct clone
and processed into a session message.
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel, JSON


class EmailMessageBase(SQLModel):
    """Shared fields for email message."""
    email_message_id: str = Field(max_length=512)  # Message-ID from email headers
    sender: str = Field(max_length=320)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(default="", sa_column=Column(Text))
    references: str | None = Field(default=None, sa_column=Column(Text))
    in_reply_to: str | None = Field(default=None, max_length=512)
    received_at: datetime = Field(default_factory=datetime.utcnow)


class EmailMessage(EmailMessageBase, table=True):
    """Database table for incoming email messages."""
    __tablename__ = "email_message"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    clone_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )

    # Processing state
    processed: bool = Field(default=False)
    processing_error: str | None = Field(default=None, sa_column=Column(Text))
    pending_clone_creation: bool = Field(default=False)

    # Attachment metadata (JSON list of {filename, content_type, size})
    attachments_metadata: list | None = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EmailMessagePublic(EmailMessageBase):
    """Public representation of an email message."""
    id: uuid.UUID
    agent_id: uuid.UUID
    clone_agent_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    processed: bool = False
    processing_error: str | None = None
    pending_clone_creation: bool = False
    attachments_metadata: list | None = None
    created_at: datetime
    updated_at: datetime
