import uuid
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# AgenticTeam
# ---------------------------------------------------------------------------

class AgenticTeamBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=50)


class AgenticTeamCreate(AgenticTeamBase):
    task_prefix: str | None = Field(default=None, max_length=10)


class AgenticTeamUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=50)
    task_prefix: str | None = Field(default=None, max_length=10)


class AgenticTeam(AgenticTeamBase, table=True):
    __tablename__ = "agentic_team"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # Task prefix for short-code generation (e.g., "HR" → HR-1, HR-2)
    # When NULL, tasks in this team use the default "TASK" prefix
    task_prefix: str | None = Field(default=None, max_length=10)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AgenticTeamPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    icon: str | None
    task_prefix: str | None = None
    created_at: datetime
    updated_at: datetime


class AgenticTeamsPublic(SQLModel):
    data: list[AgenticTeamPublic]
    count: int


# ---------------------------------------------------------------------------
# AgenticTeamNode
# ---------------------------------------------------------------------------

class AgenticTeamNodeBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    node_type: str = Field(default="agent", max_length=50)
    is_lead: bool = Field(default=False)
    agent_id: uuid.UUID
    pos_x: float = Field(default=0.0)
    pos_y: float = Field(default=0.0)


class AgenticTeamNodeCreate(SQLModel):
    """
    Create request for a team node.
    name is NOT included — it is auto-populated from agent.name at service layer.
    """
    agent_id: uuid.UUID
    is_lead: bool = Field(default=False)
    pos_x: float = Field(default=0.0)
    pos_y: float = Field(default=0.0)


class AgenticTeamNodeUpdate(SQLModel):
    """
    Only is_lead, pos_x, and pos_y are updatable.
    name and agent_id cannot be changed — delete and re-add to change agent.
    """
    is_lead: bool | None = None
    pos_x: float | None = None
    pos_y: float | None = None


class AgenticTeamNode(AgenticTeamNodeBase, table=True):
    __tablename__ = "agentic_team_node"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    team_id: uuid.UUID = Field(
        foreign_key="agentic_team.id", nullable=False, ondelete="CASCADE"
    )
    # Re-declare agent_id with FK constraint (overrides base plain field)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AgenticTeamNodePublic(SQLModel):
    id: uuid.UUID
    team_id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    agent_ui_color_preset: str | None
    node_type: str
    is_lead: bool
    pos_x: float
    pos_y: float
    created_at: datetime
    updated_at: datetime


class AgenticTeamNodesPublic(SQLModel):
    data: list[AgenticTeamNodePublic]
    count: int


class AgenticTeamNodePositionUpdate(SQLModel):
    id: uuid.UUID
    pos_x: float
    pos_y: float


# ---------------------------------------------------------------------------
# AgenticTeamConnection
# ---------------------------------------------------------------------------

class AgenticTeamConnectionBase(SQLModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    connection_prompt: str = Field(default="", max_length=2000)
    enabled: bool = Field(default=True)


class AgenticTeamConnectionCreate(AgenticTeamConnectionBase):
    pass


class AgenticTeamConnectionUpdate(SQLModel):
    connection_prompt: str | None = None
    enabled: bool | None = None


class AgenticTeamConnection(AgenticTeamConnectionBase, table=True):
    __tablename__ = "agentic_team_connection"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    team_id: uuid.UUID = Field(
        foreign_key="agentic_team.id", nullable=False, ondelete="CASCADE"
    )
    # Re-declare with FK constraints (override base plain fields)
    source_node_id: uuid.UUID = Field(
        foreign_key="agentic_team_node.id", nullable=False, ondelete="CASCADE"
    )
    target_node_id: uuid.UUID = Field(
        foreign_key="agentic_team_node.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AgenticTeamConnectionPublic(SQLModel):
    id: uuid.UUID
    team_id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    source_node_name: str
    target_node_name: str
    source_node_color_preset: str | None = None
    target_node_color_preset: str | None = None
    connection_prompt: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class GenerateConnectionPromptResponse(SQLModel):
    """Response from AI connection prompt generation."""
    success: bool
    connection_prompt: str | None = None
    error: str | None = None


class AgenticTeamConnectionsPublic(SQLModel):
    data: list[AgenticTeamConnectionPublic]
    count: int


# ---------------------------------------------------------------------------
# Bulk chart response
# ---------------------------------------------------------------------------

class AgenticTeamChartPublic(SQLModel):
    team: AgenticTeamPublic
    nodes: list[AgenticTeamNodePublic]
    connections: list[AgenticTeamConnectionPublic]
