import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import Text, UniqueConstraint


# ---------------------------------------------------------------------------
# Database tables
# ---------------------------------------------------------------------------


class AppAgentRoute(SQLModel, table=True):
    """Route that binds an agent to users with routing rules.

    Can be created by any user for their own agents, or by admins for any agent.
    """

    __tablename__ = "app_agent_route"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=255)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)
    session_mode: str = Field(max_length=20, default="conversation")
    trigger_prompt: str = Field(sa_column=Column(Text, nullable=False))
    message_patterns: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    prompt_examples: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    channel_app_mcp: bool = Field(default=True)
    is_active: bool = Field(default=True)
    auto_enable_for_users: bool = Field(default=False)
    # True when this route was created by ``InstallService`` on bundle
    # install. ``apply_update`` refreshes ``trigger_prompt``/``name``/
    # ``session_mode`` from the new revision only while this flag is
    # ``True``. Any user-driven PUT via the public update endpoint flips
    # it to ``False`` so manual edits are never overwritten.
    is_auto_managed: bool = Field(default=False)
    created_by: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AppAgentRouteAssignment(SQLModel, table=True):
    """Links a route to a specific user with a per-user enable/disable toggle."""

    __tablename__ = "app_agent_route_assignment"
    __table_args__ = (
        UniqueConstraint("route_id", "user_id", name="uq_app_agent_route_assignment"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    route_id: uuid.UUID = Field(foreign_key="app_agent_route.id", ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    is_enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserAppAgentRoute(SQLModel, table=True):
    """User-created personal agent route (configured in Settings). Soft-deprecated."""

    __tablename__ = "user_app_agent_route"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_id", name="uq_user_app_agent_route"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    session_mode: str = Field(max_length=20, default="conversation")
    trigger_prompt: str = Field(sa_column=Column(Text, nullable=False))
    message_patterns: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    channel_app_mcp: bool = Field(default=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Pydantic schemas — app agent routes
# ---------------------------------------------------------------------------


class AppAgentRouteCreate(SQLModel):
    name: str
    agent_id: uuid.UUID
    session_mode: str = "conversation"
    trigger_prompt: str
    message_patterns: str | None = None
    prompt_examples: str | None = None
    channel_app_mcp: bool = True
    is_active: bool = True
    auto_enable_for_users: bool = False
    activate_for_myself: bool = False
    assigned_user_ids: list[uuid.UUID] = []
    # ``is_auto_managed`` intentionally NOT exposed here — it's an
    # internal flag set by ``InstallService`` (and the Phase 8 backfill
    # script) via the explicit ``auto_managed=`` kwarg on
    # ``AppAgentRouteService.create_route``. Allowing it through the
    # public POST body would let users mint "bundle-managed" routes
    # that the apply-update path would happily overwrite on the next
    # revision push.


class AppAgentRouteUpdate(SQLModel):
    name: str | None = None
    session_mode: str | None = None
    trigger_prompt: str | None = None
    message_patterns: str | None = None
    prompt_examples: str | None = None
    channel_app_mcp: bool | None = None
    is_active: bool | None = None
    auto_enable_for_users: bool | None = None


class AppAgentRouteAssignmentPublic(SQLModel):
    id: uuid.UUID
    route_id: uuid.UUID
    user_id: uuid.UUID
    is_enabled: bool
    created_at: datetime


class AppAgentRoutePublic(SQLModel):
    id: uuid.UUID
    name: str
    agent_id: uuid.UUID
    agent_name: str = ""
    session_mode: str
    trigger_prompt: str
    message_patterns: str | None
    prompt_examples: str | None = None
    channel_app_mcp: bool
    is_active: bool
    auto_enable_for_users: bool = False
    is_auto_managed: bool = False
    agent_owner_name: str = ""
    agent_owner_email: str = ""
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    assignments: list[AppAgentRouteAssignmentPublic] = []


# ---------------------------------------------------------------------------
# Pydantic schemas — user personal routes (soft-deprecated)
# ---------------------------------------------------------------------------


class UserAppAgentRouteCreate(SQLModel):
    agent_id: uuid.UUID
    session_mode: str = "conversation"
    trigger_prompt: str
    message_patterns: str | None = None
    channel_app_mcp: bool = True
    is_active: bool = True


class UserAppAgentRouteUpdate(SQLModel):
    session_mode: str | None = None
    trigger_prompt: str | None = None
    message_patterns: str | None = None
    channel_app_mcp: bool | None = None
    is_active: bool | None = None


class UserAppAgentRoutePublic(SQLModel):
    id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str = ""
    session_mode: str
    trigger_prompt: str
    message_patterns: str | None
    channel_app_mcp: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Pydantic schemas — combined user response
# ---------------------------------------------------------------------------


class SharedRoutePublic(SQLModel):
    """Route shared with a user (via assignment), as seen by the assignee."""

    route_id: uuid.UUID
    name: str
    agent_name: str
    agent_owner_name: str = ""
    agent_owner_email: str = ""
    shared_by_name: str = ""
    session_mode: str
    trigger_prompt: str
    message_patterns: str | None = None
    prompt_examples: str | None = None
    is_active: bool  # route-level toggle (set by route creator)
    assignment_id: uuid.UUID
    is_enabled: bool  # user-level toggle


class UserAppAgentRoutesResponse(SQLModel):
    """Combined response for user's personal + shared routes."""

    personal_routes: list[UserAppAgentRoutePublic]
    shared_routes: list[SharedRoutePublic]


# ---------------------------------------------------------------------------
# Conflict detection — install-time fuzzy match against effective routes
# ---------------------------------------------------------------------------


class RouteConflictMatch(SQLModel):
    """A single conflicting effective route the installer already has.

    Surfaced as a non-blocking toast on the install completion page when an
    agent's auto-created route looks similar (by lowercased token overlap)
    to another route already active for the installer. Helps the user
    spot near-duplicate intents (e.g. "Calendar Planner" vs "Vacation
    Planner") that could confuse the App MCP router.
    """

    route_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    trigger_prompt: str
    similarity: float  # 0.0 — 1.0 (Jaccard token overlap)


class RouteConflictResponse(SQLModel):
    """Response payload for the install-time conflict check.

    ``matches`` is sorted by descending similarity. Empty when no
    effective route crosses the similarity threshold (or when the agent
    has no auto-managed route to compare against).
    """

    matches: list[RouteConflictMatch] = []
