import uuid
from datetime import datetime, timezone
from typing import Literal
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

class UserDashboardBlockBase(SQLModel):
    agent_id: uuid.UUID
    view_type: Literal["webapp", "latest_session", "latest_tasks", "agent_env_file"] = Field(default="latest_session")
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
    view_type: Literal["webapp", "latest_session", "latest_tasks", "agent_env_file"] | None = None
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
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    blocks: list[UserDashboardBlock] = Relationship(
        back_populates="dashboard",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class UserDashboardCreate(UserDashboardBase):
    pass


class UserDashboardUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    sort_order: int | None = None


class UserDashboardPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    sort_order: int
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
