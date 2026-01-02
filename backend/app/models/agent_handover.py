import uuid
from datetime import datetime
from sqlmodel import Field, Relationship, SQLModel


class AgentHandoverConfig(SQLModel, table=True):
    """
    Agent handover configuration.

    Defines when and how to trigger another agent with a handover prompt.
    Each config links source agent to target agent with conditions and prompt template.

    Relationship: Many AgentHandoverConfig → One Agent (source)
                 Many AgentHandoverConfig → One Agent (target)
    """
    __tablename__ = "agent_handover_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Source agent (the one doing the handover)
    source_agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Target agent (the one receiving the handover)
    target_agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Handover prompt (when and how to trigger target agent)
    handover_prompt: str

    # Enable/disable without deleting
    enabled: bool = Field(default=True)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    source_agent: "Agent" = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[AgentHandoverConfig.source_agent_id]",
            "lazy": "joined"
        }
    )
    target_agent: "Agent" = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[AgentHandoverConfig.target_agent_id]",
            "lazy": "joined"
        }
    )


class HandoverConfigCreate(SQLModel):
    """Request to create handover configuration."""
    target_agent_id: uuid.UUID
    handover_prompt: str = ""


class HandoverConfigUpdate(SQLModel):
    """Request to update handover configuration."""
    handover_prompt: str | None = None
    enabled: bool | None = None


class HandoverConfigPublic(SQLModel):
    """Public response model for AgentHandoverConfig."""
    id: uuid.UUID
    source_agent_id: uuid.UUID
    target_agent_id: uuid.UUID
    target_agent_name: str  # Included for UI display
    handover_prompt: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class HandoverConfigsPublic(SQLModel):
    """List of handover configurations."""
    data: list[HandoverConfigPublic]
    count: int


class GenerateHandoverPromptRequest(SQLModel):
    """Request to generate handover prompt using AI."""
    target_agent_id: uuid.UUID


class GenerateHandoverPromptResponse(SQLModel):
    """Response from AI handover prompt generation."""
    success: bool
    handover_prompt: str | None = None
    error: str | None = None
