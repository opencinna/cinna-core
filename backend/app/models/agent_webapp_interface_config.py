"""
Agent Webapp Interface Config model.

Per-agent configuration for the webapp interface appearance.
One config per agent, shared across all webapp share links.
Controls UI elements like header visibility and chat widget.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentWebappInterfaceConfigBase(SQLModel):
    show_header: bool = True
    chat_mode: str | None = None  # "conversation" | "building" | None (disabled)


class AgentWebappInterfaceConfigCreate(AgentWebappInterfaceConfigBase):
    pass


class AgentWebappInterfaceConfigUpdate(SQLModel):
    show_header: bool | None = None
    chat_mode: str | None = None  # "conversation" | "building" | None (disabled)


class AgentWebappInterfaceConfig(AgentWebappInterfaceConfigBase, table=True):
    __tablename__ = "agent_webapp_interface_config"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_webapp_interface_config_agent_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    chat_mode: str | None = Field(default=None, max_length=20)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentWebappInterfaceConfigPublic(AgentWebappInterfaceConfigBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
