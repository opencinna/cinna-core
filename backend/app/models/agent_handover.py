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

    # Auto-trigger source agent when sub-task reports state
    auto_feedback: bool = Field(default=True)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    source_agent: "Agent" = Relationship(
        back_populates="handover_configs",
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
    auto_feedback: bool | None = None


class HandoverConfigPublic(SQLModel):
    """Public response model for AgentHandoverConfig."""
    id: uuid.UUID
    source_agent_id: uuid.UUID
    target_agent_id: uuid.UUID
    target_agent_name: str  # Included for UI display
    handover_prompt: str
    enabled: bool
    auto_feedback: bool
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


class CreateAgentTaskRequest(SQLModel):
    """
    Request to create a task (with or without target agent).

    If target_agent_id is provided: Direct handover (task auto-executes)
    If target_agent_id is None: Inbox task (user reviews and executes manually)
    """
    task_message: str
    target_agent_id: uuid.UUID | None = None
    target_agent_name: str | None = None
    source_session_id: uuid.UUID


class CreateAgentTaskResponse(SQLModel):
    """Response from task creation."""
    success: bool
    task_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None  # Only set for direct handover (auto-executed)
    message: str | None = None
    error: str | None = None


# Backward compatibility aliases
class ExecuteHandoverRequest(CreateAgentTaskRequest):
    """
    Deprecated: Use CreateAgentTaskRequest instead.
    Request to execute a handover to another agent.
    """
    # Map old field names to new ones
    @property
    def handover_message(self) -> str:
        return self.task_message

    @classmethod
    def model_validate(cls, obj, **kwargs):
        # Handle old field name
        if isinstance(obj, dict) and "handover_message" in obj and "task_message" not in obj:
            obj = obj.copy()
            obj["task_message"] = obj.pop("handover_message")
        return super().model_validate(obj, **kwargs)


class ExecuteHandoverResponse(CreateAgentTaskResponse):
    """
    Deprecated: Use CreateAgentTaskResponse instead.
    Response from handover execution.
    """
    pass


class UpdateSessionStateRequest(SQLModel):
    """Request to update session state from agent-env."""
    session_id: str  # Backend session ID
    state: str  # "completed" | "needs_input" | "error"
    summary: str  # Result/question/error description


class UpdateSessionStateResponse(SQLModel):
    """Response from session state update."""
    success: bool
    message: str | None = None
    error: str | None = None


class RespondToTaskRequest(SQLModel):
    """Request to respond to a sub-task from source agent."""
    task_id: str  # Sub-task ID
    message: str  # Message for target agent
    source_session_id: str  # For auth verification
