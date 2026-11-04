"""Agent dashboard components: cached tile results, the per-target runtime row, API shapes.

An ``agent_component`` dashboard block shows a component an agent keeps in
``dashboards/<name>/`` (or inside one of its skills). The block's config lives
on ``user_dashboard_block.config`` (``AgentComponentBlockConfig``); these tables
hold what the block computed and what the owner's open page reported:

- ``user_dashboard_block_result`` — the block's last good result (1:1 with the
  block), the params it was computed with, and the last failure sentence.
- ``dashboard_tile_runtime`` — keyed by a budget/frame *target* (``b:<block>``
  or ``p:<user>:<agent>:<skill|->:<component>:<tile_key|preview>``): the run
  times of the last minute (shared by every worker, read under
  ``SELECT … FOR UPDATE``) and the page's per-document highlight readiness.
"""
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import BigInteger, Column, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class UserDashboardBlockResult(SQLModel, table=True):
    __tablename__ = "user_dashboard_block_result"

    block_id: uuid.UUID = Field(
        foreign_key="user_dashboard_block.id", primary_key=True, ondelete="CASCADE"
    )
    # One JSON value as text (at most 5 MiB: env-core caps a run's stdout there).
    result: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    fetched_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    params_hash: str | None = Field(default=None, max_length=64)
    # The last failure, as one sentence; the cached result stays shown under it.
    mark: str | None = Field(default=None, max_length=500)
    mark_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    # The settled schedule run a schedule-fed tile last recomputed for.
    consumed_schedule_log_id: uuid.UUID | None = Field(default=None, nullable=True)


class DashboardTileRuntime(SQLModel, table=True):
    __tablename__ = "dashboard_tile_runtime"
    __table_args__ = (Index("ix_dashboard_tile_runtime_updated_at", "updated_at"),)

    target_key: str = Field(primary_key=True, max_length=400)
    # Epoch seconds of the counted runs of the last minute (at most 30 kept).
    run_times: list[float] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    page_doc: str | None = Field(default=None, max_length=64)
    highlight_ready: bool = Field(default=False)
    # The page's monotonic sequence of its last page-state write (older ones
    # are ignored) and when it was written (readiness expires after 15 min).
    page_seq: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    page_state_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    updated_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ---------------------------------------------------------------------------
# API shapes
# ---------------------------------------------------------------------------

DashboardComponentErrorCode = Literal[
    "rate_limited",
    "needs_credentials",
    "call_failed",
    "invalid_input",
    "not_found",
    "env_unavailable",
    "adapter_unsupported",
]

TileRefreshReason = Literal["open", "reshow", "manual", "interval", "after_turn", "params"]


class DashboardComponentErrorDetail(SQLModel):
    code: DashboardComponentErrorCode
    message: str
    retry_after_s: int | None = None
    field: str | None = None


class DashboardComponentErrorBody(SQLModel):
    """Error body of every agent-component route: ``{error: {code, message, …}}``."""

    error: DashboardComponentErrorDetail


class DashboardComponentFinding(SQLModel):
    path: str
    code: str
    field: str | None = None
    severity: Literal["error", "warning"]
    message: str


class DashboardComponentPlacement(SQLModel):
    """A block on one of the owner's dashboards that shows the component."""

    dashboard_id: uuid.UUID
    dashboard_name: str
    block_id: uuid.UUID
    # ``desktop``: a Cinna Desktop backing dashboard (not shown on the web).
    origin: str = "web"


class DashboardComponentPublic(SQLModel):
    name: str
    skill: str | None = None
    title: str | None = None
    view: str | None = None
    # component.json as written (None when it does not parse).
    component_json: dict[str, Any] | None = None
    findings: list[DashboardComponentFinding] = []
    has_errors: bool = False
    size_bytes: int = 0
    placements: list[DashboardComponentPlacement] = []


class DashboardComponentsPublic(SQLModel):
    components: list[DashboardComponentPublic]


class DashboardComponentRunRequest(SQLModel):
    skill: str | None = Field(default=None, max_length=128)
    # None runs the component's source.
    endpoint: str | None = Field(default=None, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    # The desktop's own tile id for a remote tile; None for a core preview.
    tile_key: str | None = Field(default=None, max_length=64)


class DashboardComponentRunResult(SQLModel):
    value: Any = None
    fetched_at: datetime
    notice: str | None = None
    params_resolved: dict[str, Any] = {}


class DashboardComponentFrameRequest(SQLModel):
    skill: str | None = Field(default=None, max_length=128)
    tile_key: str | None = Field(default=None, max_length=64)


class DashboardFramePublic(SQLModel):
    frame_url: str
    expires_at: datetime


class DashboardTileDataPublic(SQLModel):
    """A tile's cached data and what the page needs to draw it."""

    data: Any = None
    stale: bool = True
    fetched_at: datetime | None = None
    mark: str | None = None
    mark_at: datetime | None = None
    notice: str | None = None
    params_resolved: dict[str, Any] = {}
    missing_credentials: list[str] = []
    # Set when a refresh did not run, e.g. ``env_asleep`` for an interval tick.
    skipped: str | None = None


class DashboardTileRefreshRequest(SQLModel):
    reason: TileRefreshReason


class DashboardTileParamsRequest(SQLModel):
    """The tile's whole param set: keys left out (or cleared) go back to their default."""

    params: dict[str, Any] = Field(default_factory=dict)


class DashboardTileCallRequest(SQLModel):
    endpoint: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class DashboardTileCallResult(SQLModel):
    value: Any = None


class DashboardTilePageStateRequest(SQLModel):
    page_doc: str | None = Field(default=None, max_length=64)
    highlight_ready: bool = False
    # Monotonic per tile, also across tab reloads (e.g. ``Date.now() * 1000 +
    # counter``): a write with a lower ``seq`` than the stored one is ignored.
    seq: int | None = Field(default=None, ge=0, le=2**63 - 1)
    # Conditional write (a tab withdrawing its own readiness): applied only
    # while the stored ``page_doc`` is this one, so another tab's state stays.
    if_page_doc: str | None = Field(default=None, max_length=64)
