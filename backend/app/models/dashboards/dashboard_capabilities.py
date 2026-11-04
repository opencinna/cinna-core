"""API shape of ``GET /dashboards/capabilities``.

What a client (the cinna CLI's ``dashboards`` verbs, first of all) may rely on
before it calls any dashboard route. Every number is read from the constant
that enforces it; see ``app/services/dashboards/capabilities.py``.
"""
from sqlmodel import SQLModel


class DashboardsLimits(SQLModel):
    runs_per_minute: int
    blocks_per_dashboard: int
    # web/cloud dashboards cap; desktop-origin boards use MAX_DESKTOP_DASHBOARDS_PER_USER
    dashboards_per_user: int
    context_chars: int
    summary_chars: int
    stdout_bytes: int
    timeout_s_default: int
    timeout_s_max: int


class DashboardsCapabilities(SQLModel):
    # Set from services/dashboards/capabilities.CAPABILITIES_VERSION.
    version: int
    # The Local Agent Kit contract version the components follow; ``None`` only
    # when this instance cannot serve the kit contract.
    contract_version: str | None
    limits: DashboardsLimits
    chat: bool = True
    component_refs: bool = True
