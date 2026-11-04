"""API shapes of the dashboard chat, the agent tile tools and component references.

- Dashboard chat (D-C5): one conversation session per (dashboard, agent),
  found through ``session.dashboard_id``.
- Agent tile tools (``dashboards_tiles`` / ``dashboards_update_tile``): the
  env-core client posts ``{session_id, …}`` and always gets HTTP 200, either the
  result object or ``{error: {code, message, …}}`` with the codes of
  ``dashboards-tools.schema.json``.
- Component references (``cinna://component/<agent>/[<skill>/]<name>``):
  resolved for rendering a card; resolving never wakes an environment.
"""
import uuid
from typing import Any, Literal

from sqlmodel import Field, SQLModel

from app.models.dashboards.dashboard_component import DashboardComponentFinding
from app.models.sessions.session import SessionPublic

# ---------------------------------------------------------------------------
# Dashboard chat
# ---------------------------------------------------------------------------


class DashboardChatCreate(SQLModel):
    agent_id: uuid.UUID
    # Start a new chat even when one exists ("New chat").
    new: bool = False


class DashboardChatPublic(SQLModel):
    """The (dashboard, agent) chat session; ``None`` before the first one exists."""

    session: SessionPublic | None = None


# ---------------------------------------------------------------------------
# Agent tile tools
# ---------------------------------------------------------------------------

AgentTileToolErrorCode = Literal["not_found", "invalid_input", "rate_limited", "no_dashboard"]


class AgentTileToolErrorDetail(SQLModel):
    code: AgentTileToolErrorCode
    message: str
    field: str | None = None
    retry_after_s: int | None = None


class AgentTileToolError(SQLModel):
    error: AgentTileToolErrorDetail


# ``session_id`` is the chat's backend session id; anything that is not one of
# the agent's sessions answers ``no_dashboard`` (so it is a plain string here).
class AgentDashboardTilesRequest(SQLModel):
    session_id: str = Field(max_length=64)


class AgentDashboardUpdateTileRequest(SQLModel):
    session_id: str = Field(max_length=64)
    # Loosely typed on purpose: a malformed value answers 200 ``not_found`` /
    # ``invalid_input`` (the tool contract), never a 422.
    tile_id: Any = None
    params: Any = None
    highlight: Any = None


class AgentDashboardRef(SQLModel):
    id: uuid.UUID
    name: str


class AgentDashboardTile(SQLModel):
    tile_id: str
    component: str
    skill: str | None = None
    title: str
    view: str
    params: dict[str, Any]
    param_specs: dict[str, Any]
    can_highlight: bool
    # Only for the tile focused in the last <dashboard_context> of the chat.
    summary: str | None = None
    selection: list[str] | None = None


class AgentDashboardTilesResult(SQLModel):
    dashboard: AgentDashboardRef
    tiles: list[AgentDashboardTile]


class AgentDashboardUpdateTileResult(SQLModel):
    tile_id: str
    params: dict[str, Any]
    recomputing: bool
    highlighted: bool


# ---------------------------------------------------------------------------
# Component references
# ---------------------------------------------------------------------------

ComponentRefStatus = Literal[
    "ok", "ambiguous", "not_installed", "gone", "invalid", "env_asleep", "adapter_unsupported"
]

MAX_COMPONENT_REFS = 20


class ComponentRefsResolveRequest(SQLModel):
    refs: list[str] = Field(max_length=MAX_COMPONENT_REFS)
    # The chat the references were printed in: its agent wins a ``bundle:``
    # match, and only it can match ``id:`` / slug references.
    session_id: uuid.UUID | None = None


class ComponentRefAgent(SQLModel):
    id: uuid.UUID
    name: str


class ComponentRefComponent(SQLModel):
    name: str
    skill: str | None = None
    title: str | None = None
    view: str | None = None


class ComponentRefPlacement(SQLModel):
    dashboard_id: uuid.UUID
    name: str


class ComponentRefResolved(SQLModel):
    ref: str
    status: ComponentRefStatus
    # One sentence for every status but ``ok``.
    message: str | None = None
    agent: ComponentRefAgent | None = None
    component: ComponentRefComponent | None = None
    from_skill: str | None = None
    findings: list[DashboardComponentFinding] = []
    placements: list[ComponentRefPlacement] = []
    # ``not_installed`` only: the bundle's catalog name.
    catalog_name: str | None = None


class ComponentRefsResolved(SQLModel):
    results: list[ComponentRefResolved]
