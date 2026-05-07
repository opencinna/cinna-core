"""
Agent Webhook Log models.

Immutable, append-only record of every webhook invocation attempt. Preserved
(with session_id set to NULL) when the associated session is deleted.

Status values:
- "session_started": session-type webhook — session created, message queued.
- "success": script-type webhook — command exited with code 0.
- "script_error": script-type webhook — command exited with a non-zero code
  (still a "normal" outcome: logged, not an infrastructure failure).
- "error": infrastructure failure (env not available, timeout, internal
  exception, session-creation failure).
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import Text, JSON, Index
from sqlmodel import Field, SQLModel


class AgentWebhookLog(SQLModel, table=True):
    """One invocation record of a configured agent webhook."""
    __tablename__ = "agent_webhook_log"
    __table_args__ = (
        Index("ix_agent_webhook_log_webhook_fk", "webhook_id_fk"),
        Index("ix_agent_webhook_log_agent_id", "agent_id"),
        Index("ix_agent_webhook_log_executed_at", "executed_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # FK column is named ``webhook_id_fk`` to avoid clashing with the public
    # ``webhook_id`` slug column on the parent ``agent_webhook`` row.
    webhook_id_fk: uuid.UUID = Field(
        foreign_key="agent_webhook.id", ondelete="CASCADE"
    )
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Snapshot of webhook.type at execution time
    webhook_type: str

    # Outcome
    status: str  # "session_started" | "success" | "script_error" | "error"

    # Request metadata
    remote_ip: str | None = Field(default=None, max_length=64)
    headers_subset: dict | None = Field(default=None, sa_type=JSON)
    payload_received: str | None = Field(default=None, sa_type=Text)
    payload_content_type: str | None = Field(default=None)

    # Session-type fields
    prompt_used: str | None = Field(default=None, sa_type=Text)

    # Script-type fields
    command_executed: str | None = Field(default=None, sa_type=Text)
    command_output: str | None = Field(default=None, sa_type=Text)
    command_stderr: str | None = Field(default=None, sa_type=Text)
    command_exit_code: int | None = Field(default=None)

    # Session created (if any). SET NULL on session delete → log persists.
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )

    # Error details for status="error"
    error_message: str | None = Field(default=None, sa_type=Text)

    # End-to-end handler duration
    duration_ms: int | None = Field(default=None)

    # When execution happened (UTC)
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentWebhookLogPublic(SQLModel):
    """Public log response."""
    id: uuid.UUID
    webhook_id_fk: uuid.UUID
    agent_id: uuid.UUID
    webhook_type: str
    status: str
    remote_ip: str | None
    headers_subset: dict | None
    payload_received: str | None
    payload_content_type: str | None
    prompt_used: str | None
    command_executed: str | None
    command_output: str | None
    command_stderr: str | None
    command_exit_code: int | None
    session_id: uuid.UUID | None
    error_message: str | None
    duration_ms: int | None
    executed_at: datetime


class AgentWebhookLogsPublic(SQLModel):
    """List response."""
    data: list[AgentWebhookLogPublic]
    count: int
