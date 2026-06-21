import uuid
from datetime import datetime, UTC

import sqlalchemy as sa
from sqlalchemy import DateTime, Index, String, Text
from sqlmodel import Column, Field, SQLModel


class AgentEnvActionLog(SQLModel, table=True):
    """Immutable, append-only log of agent-environment operations.

    Mirrors ``AgentScheduleLog``'s shape (append-only, free-string ``status``,
    indexed by parent + ``executed_at``). Captures full, untruncated error
    detail for env operations that have no schedule parent (rebuild, setup,
    package install, credential/file sync, cron-skip). The source for the
    env-card "Show details" affordance and the link between the env's
    ``critical_state`` flag and the originating failure.

    Append-only; never updated; deleted only via cascade.

    Status values (free string, matching AgentScheduleLog convention):
    - "success": the operation completed
    - "error": the operation failed (full detail captured)
    - "skipped": a scheduled run was skipped because the env is critical

    Detail is operational text only (uv resolver output, exception strings).
    It must NEVER contain secrets/credential payloads — credential-sync errors
    log only the failure reason/status, never the payload.
    """

    __tablename__ = "agent_env_action_log"
    __table_args__ = (
        Index("ix_agent_env_action_log_environment_id", "environment_id"),
        Index("ix_agent_env_action_log_agent_id", "agent_id"),
        Index("ix_agent_env_action_log_executed_at", "executed_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    environment_id: uuid.UUID = Field(
        foreign_key="agent_environment.id", ondelete="CASCADE"
    )
    # Denormalized agent ref for owner lookups / fleet queries.
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # "rebuild" | "setup_after_rebuild" | "package_install" |
    # "system_package_install" | "credential_sync" | "file_sync" | "cron_skipped"
    action: str = Field(sa_column=Column(String(48), nullable=False))
    # "success" | "error" | "skipped"
    status: str = Field(sa_column=Column(String(24), nullable=False))
    # The critical_cause code when status="error" (links the env flag to the row).
    cause: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    # Short human-readable cause line, safe to surface in lists; seeds the email's brief cause.
    summary: str | None = Field(
        default=None, sa_column=Column(String(512), nullable=True)
    )
    # Full, untruncated error/output text. Source for "Show details".
    detail: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AgentEnvActionLogPublic(SQLModel):
    """Public response model for AgentEnvActionLog."""

    id: uuid.UUID
    environment_id: uuid.UUID
    agent_id: uuid.UUID
    action: str
    status: str
    cause: str | None
    summary: str | None
    detail: str | None
    executed_at: datetime


class AgentEnvActionLogsPublic(SQLModel):
    """List response model for AgentEnvActionLog."""

    data: list[AgentEnvActionLogPublic]
    count: int
