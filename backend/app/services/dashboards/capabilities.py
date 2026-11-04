"""What ``GET /dashboards/capabilities`` reports, read from the enforcing constants.

The endpoint runner's limits are enforced inside the agent environment by
env-core (``app/env-templates/app_core_base/core/server/endpoint_runner.py``),
which the backend cannot import. They are mirrored here; a test pins both
copies equal, so change them together.
"""
import logging

from fastapi import HTTPException

from app.models.dashboards.dashboard_capabilities import (
    DashboardsCapabilities,
    DashboardsLimits,
)
from app.services.cli.local_agent_kit_service import LocalAgentKitService
from app.services.dashboards import dashboard_context, run_budget
from app.services.users.user_dashboard_service import (
    MAX_BLOCKS_PER_DASHBOARD,
    MAX_DASHBOARDS_PER_USER,
)

logger = logging.getLogger(__name__)

CAPABILITIES_VERSION = 1

# Mirrors of env-core ``endpoint_runner.DEFAULT_TIMEOUT_S`` / ``MAX_TIMEOUT_S`` /
# ``MAX_STDOUT_BYTES``.
ENDPOINT_DEFAULT_TIMEOUT_S = 60
ENDPOINT_MAX_TIMEOUT_S = 300
ENDPOINT_MAX_STDOUT_BYTES = 5 * 1024 * 1024


def _contract_version() -> str | None:
    """The kit's ``CONTRACT_VERSION``; ``None`` when the kit contract is unservable."""
    try:
        return LocalAgentKitService.get_contract_version()
    except HTTPException:
        # Already logged by the kit service; the limits stay useful without it.
        return None


def dashboards_capabilities() -> DashboardsCapabilities:
    """Limits and features of agent dashboards; never touches an environment."""
    return DashboardsCapabilities(
        version=CAPABILITIES_VERSION,
        contract_version=_contract_version(),
        limits=DashboardsLimits(
            runs_per_minute=run_budget.MAX_RUNS_PER_WINDOW,
            blocks_per_dashboard=MAX_BLOCKS_PER_DASHBOARD,
            # web/cloud dashboards cap; desktop-origin boards use MAX_DESKTOP_DASHBOARDS_PER_USER
            dashboards_per_user=MAX_DASHBOARDS_PER_USER,
            context_chars=dashboard_context.MAX_CHARS,
            summary_chars=dashboard_context.SUMMARY_MAX_CHARS,
            stdout_bytes=ENDPOINT_MAX_STDOUT_BYTES,
            timeout_s_default=ENDPOINT_DEFAULT_TIMEOUT_S,
            timeout_s_max=ENDPOINT_MAX_TIMEOUT_S,
        ),
        chat=True,
        component_refs=True,
    )
