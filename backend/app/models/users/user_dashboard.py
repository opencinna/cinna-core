import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import StringConstraints, field_validator
from sqlmodel import Field, Relationship, SQLModel, Column
from sqlalchemy import JSON, Index


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Prompt action models
# ---------------------------------------------------------------------------

class UserDashboardBlockPromptActionBase(SQLModel):
    prompt_text: str = Field(min_length=1, max_length=2000)
    label: str | None = Field(default=None, max_length=100)
    sort_order: int = Field(default=0)


class UserDashboardBlockPromptAction(UserDashboardBlockPromptActionBase, table=True):
    __tablename__ = "user_dashboard_block_prompt_action"
    __table_args__ = (
        Index("ix_user_dashboard_block_prompt_action_block_id", "block_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    block_id: uuid.UUID = Field(
        foreign_key="user_dashboard_block.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    block: "UserDashboardBlock" = Relationship(back_populates="prompt_actions")


class UserDashboardBlockPromptActionCreate(UserDashboardBlockPromptActionBase):
    pass


class UserDashboardBlockPromptActionUpdate(SQLModel):
    prompt_text: str | None = Field(default=None, min_length=1, max_length=2000)
    label: str | None = None
    sort_order: int | None = None


class UserDashboardBlockPromptActionPublic(SQLModel):
    id: uuid.UUID
    block_id: uuid.UUID
    prompt_text: str
    label: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Block models
# ---------------------------------------------------------------------------

DashboardBlockViewType = Literal[
    "webapp", "latest_session", "latest_tasks", "agent_env_file", "agent_component"
]

# A duration as component.json writes it (``30s``, ``15m``, ``6h``, ``1d``).
DURATION_PATTERN = r"^[1-9][0-9]*(s|m|h|d)$"
# At least one minute: ``while_open_every`` never polls faster than that.
DURATION_1M_PATTERN = r"^(([6-9][0-9]|[1-9][0-9]{2,})s|[1-9][0-9]*(m|h|d))$"
COMPONENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
SKILL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class AgentComponentRefreshOverrides(SQLModel):
    """The Edit-tile overrides of a component's ``refresh`` (``on_app_start`` is never stored)."""

    model_config = {"extra": "forbid"}

    max_age: Annotated[str, StringConstraints(pattern=DURATION_PATTERN)] | None = None
    # A duration of at least 1m, or "manual" for "never while open".
    while_open_every: str | None = Field(default=None)
    after_turn: bool | None = None

    @field_validator("while_open_every")
    @classmethod
    def _check_while_open_every(cls, value: str | None) -> str | None:
        if value is None or value == "manual" or re.fullmatch(DURATION_1M_PATTERN, value):
            return value
        raise ValueError("while_open_every must be a duration of at least 1m, or \"manual\"")


class AgentComponentBlockConfig(SQLModel):
    """``config`` of an ``agent_component`` block."""

    model_config = {"extra": "forbid"}

    # sqlmodel drops ``regex=`` silently, so the patterns ride on the types.
    component: Annotated[str, StringConstraints(pattern=COMPONENT_NAME_PATTERN)]
    skill: Annotated[str, StringConstraints(pattern=SKILL_NAME_PATTERN)] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    refresh: AgentComponentRefreshOverrides = Field(
        default_factory=AgentComponentRefreshOverrides
    )

class UserDashboardBlockBase(SQLModel):
    agent_id: uuid.UUID
    view_type: DashboardBlockViewType = Field(default="latest_session")
    title: str | None = Field(default=None, max_length=255)
    show_border: bool = Field(default=True)
    show_header: bool = Field(default=False)
    grid_x: int = Field(default=0, ge=0)
    grid_y: int = Field(default=0, ge=0)
    grid_w: int = Field(default=2, ge=1)
    grid_h: int = Field(default=2, ge=1)
    config: dict | None = None


class UserDashboardBlock(UserDashboardBlockBase, table=True):
    __tablename__ = "user_dashboard_block"
    __table_args__ = (
        Index("ix_user_dashboard_block_dashboard_id", "dashboard_id"),
        Index("ix_user_dashboard_block_agent_id", "agent_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", nullable=False, ondelete="CASCADE")
    view_type: str = Field(default="latest_session", max_length=50)
    dashboard_id: uuid.UUID = Field(
        foreign_key="user_dashboard.id", nullable=False, ondelete="CASCADE"
    )
    config: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    dashboard: "UserDashboard" = Relationship(back_populates="blocks")
    prompt_actions: list["UserDashboardBlockPromptAction"] = Relationship(
        back_populates="block",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "UserDashboardBlockPromptAction.sort_order",
        },
    )


class UserDashboardBlockCreate(UserDashboardBlockBase):
    pass


class UserDashboardBlockUpdate(SQLModel):
    view_type: DashboardBlockViewType | None = None
    title: str | None = None
    show_border: bool | None = None
    show_header: bool | None = None
    grid_x: int | None = Field(default=None, ge=0)
    grid_y: int | None = Field(default=None, ge=0)
    grid_w: int | None = Field(default=None, ge=1)
    grid_h: int | None = Field(default=None, ge=1)
    config: dict | None = None


class UserDashboardBlockPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    view_type: str
    title: str | None
    show_border: bool
    show_header: bool = False
    grid_x: int
    grid_y: int
    grid_w: int
    grid_h: int
    config: dict | None
    prompt_actions: list[UserDashboardBlockPromptActionPublic] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dashboard models
# ---------------------------------------------------------------------------

# Who owns the dashboard's layout: the web app, or Cinna Desktop (a backing
# dashboard for a desktop dashboard's cloud tiles; hidden from the web list).
DashboardOrigin = Literal["web", "desktop"]
DASHBOARD_ORIGINS: tuple[str, ...] = ("web", "desktop")


class UserDashboardBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None)


class UserDashboard(UserDashboardBase, table=True):
    __tablename__ = "user_dashboard"
    __table_args__ = (
        Index("ix_user_dashboard_owner_id", "owner_id"),
        Index("ix_user_dashboard_owner_sort", "owner_id", "sort_order"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    sort_order: int = Field(default=0)
    # Set on create only (``UserDashboardCreate.origin``); never updated.
    origin: str = Field(default="web", max_length=16, sa_column_kwargs={"server_default": "web"})
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    blocks: list[UserDashboardBlock] = Relationship(
        back_populates="dashboard",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class UserDashboardCreate(UserDashboardBase):
    origin: DashboardOrigin = "web"


class UserDashboardUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    sort_order: int | None = None


class UserDashboardPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    sort_order: int
    origin: str = "web"
    created_at: datetime
    updated_at: datetime
    blocks: list[UserDashboardBlockPublic] = []

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Bulk layout update
# ---------------------------------------------------------------------------

class BlockLayoutUpdate(SQLModel):
    block_id: uuid.UUID
    grid_x: int = Field(ge=0)
    grid_y: int = Field(ge=0)
    grid_w: int = Field(ge=1)
    grid_h: int = Field(ge=1)
