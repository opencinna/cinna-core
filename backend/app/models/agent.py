import uuid
from datetime import datetime, UTC
from typing import TYPE_CHECKING, List, Optional
from sqlmodel import Field, Relationship, SQLModel, Column
from sqlalchemy import JSON, Index, ForeignKeyConstraint, text, Text

from app.models.user import User
from app.models.link_models import AgentCredentialLink

if TYPE_CHECKING:
    from app.models.agent_share import AgentShare


# Clone-related constants
class CloneMode:
    """Sharing mode for cloned agents"""
    USER = "user"  # Read-only config, no building mode
    BUILDER = "builder"  # Editable config, building mode allowed


class UpdateMode:
    """Update mode for cloned agents"""
    AUTOMATIC = "automatic"  # Updates applied automatically
    MANUAL = "manual"  # User decides when to apply updates


# Shared properties
class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    workflow_prompt: str | None = Field(default=None)
    entrypoint_prompt: str | None = Field(default=None)
    refiner_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


# Properties to receive on agent creation
class AgentCreate(AgentBase):
    description: str | None = None
    user_workspace_id: uuid.UUID | None = None


# Properties to receive on agent update
class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    refiner_prompt: str | None = None
    is_active: bool | None = None
    ui_color_preset: str | None = None
    show_on_dashboard: bool | None = None
    conversation_mode_ui: str | None = None
    a2a_config: dict | None = None
    example_prompts: list[str] | None = None
    inactivity_period_limit: str | None = None
    # Clone owners can update these
    update_mode: str | None = None  # "automatic" | "manual"


# Database model, database table inferred from class name
class Agent(AgentBase, table=True):
    __table_args__ = (
        # Partial indexes for efficient clone/update queries
        Index(
            "ix_agent_is_clone",
            "is_clone",
            postgresql_where=text("is_clone = true"),
        ),
        Index(
            "ix_agent_parent",
            "parent_agent_id",
            postgresql_where=text("parent_agent_id IS NOT NULL"),
        ),
        Index(
            "ix_agent_pending_update",
            "pending_update",
            postgresql_where=text("pending_update = true"),
        ),
        # Named foreign key for parent_agent_id
        ForeignKeyConstraint(
            ["parent_agent_id"],
            ["agent.id"],
            name="fk_agent_parent",
            ondelete="SET NULL",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    # NEW FIELDS for agent sessions
    description: str | None = None
    is_active: bool = Field(default=True)
    active_environment_id: uuid.UUID | None = Field(default=None, foreign_key="agent_environment.id")
    ui_color_preset: str | None = Field(default="slate")
    show_on_dashboard: bool = Field(default=True)
    conversation_mode_ui: str = Field(default="detailed")  # "detailed" or "compact"
    agent_sdk_config: dict = Field(default_factory=dict, sa_column=Column(JSON))  # SDK config: sdk_tools, allowed_tools
    a2a_config: dict = Field(default_factory=dict, sa_column=Column(JSON))  # A2A config: skills, version, generated_at
    example_prompts: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    inactivity_period_limit: str | None = Field(default=None)  # None="10min" | "2_days" | "1_week" | "1_month" | "always_on"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Clone relationship fields
    is_clone: bool = Field(default=False)
    parent_agent_id: uuid.UUID | None = Field(default=None)  # FK defined in __table_args__
    clone_mode: str | None = Field(default=None)  # "user" | "builder"
    last_sync_at: datetime | None = Field(default=None)

    # Update preferences (for clones)
    update_mode: str = Field(default="automatic")  # "automatic" | "manual"
    pending_update: bool = Field(default=False)
    pending_update_at: datetime | None = Field(default=None)
    last_update_status: str | None = Field(default=None)  # "synced" | "dismissed" | None

    owner: User | None = Relationship(back_populates="agents")
    credentials: List["app.models.credential.Credential"] = Relationship(
        back_populates="agents", link_model=AgentCredentialLink
    )
    schedules: List["app.models.agent_schedule.AgentSchedule"] = Relationship(
        back_populates="agent",
        cascade_delete=True
    )
    handover_configs: List["app.models.agent_handover.AgentHandoverConfig"] = Relationship(
        back_populates="source_agent",
        sa_relationship_kwargs={
            "foreign_keys": "[AgentHandoverConfig.source_agent_id]",
            "cascade": "all, delete-orphan"
        }
    )

    # Clone relationships (self-referential)
    parent_agent: Optional["Agent"] = Relationship(
        back_populates="clones",
        sa_relationship_kwargs={"remote_side": "Agent.id"}
    )
    clones: List["Agent"] = Relationship(back_populates="parent_agent")


# Properties to return via API, id is always required
class AgentPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    workflow_prompt: str | None
    entrypoint_prompt: str | None
    refiner_prompt: str | None
    is_active: bool
    active_environment_id: uuid.UUID | None
    ui_color_preset: str | None
    show_on_dashboard: bool
    conversation_mode_ui: str
    agent_sdk_config: dict | None = None
    a2a_config: dict | None = None
    example_prompts: list[str] = []
    inactivity_period_limit: str | None = None
    created_at: datetime
    updated_at: datetime
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None

    # Clone info for UI
    is_clone: bool = False
    clone_mode: str | None = None
    update_mode: str = "automatic"
    pending_update: bool = False
    last_update_status: str | None = None  # "synced" | "dismissed" | None
    parent_agent_id: uuid.UUID | None = None
    parent_agent_name: str | None = None  # Resolved from parent_agent
    shared_by_email: str | None = None  # Resolved from share record


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# Properties to return agent with credentials
class AgentWithCredentials(AgentPublic):
    credentials: list["CredentialPublic"]


# Request to link credential to agent
class AgentCredentialLinkRequest(SQLModel):
    credential_id: uuid.UUID


# Request to create agent with full flow (agent + environment + session)
class AgentCreateFlowRequest(SQLModel):
    description: str = Field(min_length=1, max_length=8000)
    mode: str = Field(default="building")  # "building" or "conversation"
    auto_create_session: bool = Field(default=False)  # If False, stop after environment is ready
    user_workspace_id: uuid.UUID | None = None
    agent_sdk_conversation: str | None = None  # SDK for conversation mode (e.g., "claude-code/anthropic")
    agent_sdk_building: str | None = None  # SDK for building mode


# Response for agent creation flow initiation
class AgentCreateFlowResponse(SQLModel):
    agent_id: uuid.UUID
    message: str


# SDK Config schemas
class AgentSdkConfig(SQLModel):
    """Schema for agent SDK configuration"""
    sdk_tools: list[str] = []  # All tools discovered from agent-env
    allowed_tools: list[str] = []  # Tools approved by user for automatic permission grant


class AllowedToolsUpdate(SQLModel):
    """Schema for updating allowed tools list"""
    tools: list[str]  # Tools to add to allowed list


class PendingToolsResponse(SQLModel):
    """Response for pending tools endpoint"""
    pending_tools: list[str]  # Tools that need approval (in sdk_tools but not in allowed_tools)
