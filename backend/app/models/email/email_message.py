"""
EmailMessage model - Stores parsed incoming emails for processing.

Each email is stored when polled from IMAP, then routed to the correct clone
and processed into a session message.
"""
import uuid
from datetime import datetime, UTC

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
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EmailMessage(EmailMessageBase, table=True):
    """Database table for incoming email messages."""
    __tablename__ = "email_message"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    #: The agent this mail was routed to — **NULL until routing says so, and
    #: NULL forever for mail that was never routed**.
    #:
    #: It was NOT NULL while the only writer was the per-agent email
    #: integration, which knew the agent before it stored anything. The email
    #: *channel* transport does not: a channel message is classified only after
    #: it arrives, and the messages worth keeping a durable record of most are
    #: exactly the ones that never get an agent — a sender denied by the
    #: whitelist, by the channel policy, or by user resolution. Those declines
    #: are deliberately silent to the sender, and the only other trace of them
    #: is ``ChannelDebugBuffer``, which is in-memory and process-local. On a
    #: transport whose senders are external by definition, silent to the sender
    #: *and* gone at the next restart is not an acceptable audit story.
    #:
    #: So the row is stored on arrival and this column is stamped afterwards,
    #: once (and if) routing produces an agent. Readers must treat NULL as
    #: "arrived, not routed" rather than as missing data.
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="CASCADE"
    )
    clone_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )
    input_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="input_task.id", ondelete="SET NULL"
    )

    # Processing state
    processed: bool = Field(default=False)
    processing_error: str | None = Field(default=None, sa_column=Column(Text))
    pending_clone_creation: bool = Field(default=False)

    # Attachment metadata (JSON list of {filename, content_type, size})
    attachments_metadata: list | None = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EmailMessagePublic(EmailMessageBase):
    """Public representation of an email message."""
    id: uuid.UUID
    #: NULL for mail that arrived but was never routed to an agent. See the
    #: column's own documentation on ``EmailMessage``.
    agent_id: uuid.UUID | None = None
    clone_agent_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    processed: bool = False
    processing_error: str | None = None
    pending_clone_creation: bool = False
    attachments_metadata: list | None = None
    created_at: datetime
    updated_at: datetime
