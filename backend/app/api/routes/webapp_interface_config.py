"""
Webapp Interface Config API Routes.

GET/PUT for per-agent webapp interface configuration (show_header, chat_mode).
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentWebappInterfaceConfigPublic,
    AgentWebappInterfaceConfigUpdate,
)
from app.services.webapp.agent_webapp_interface_config_service import (
    AgentWebappInterfaceConfigService,
    InterfaceConfigError,
)


router = APIRouter(
    prefix="/agents/{agent_id}/webapp-interface-config",
    tags=["webapp-interface-config"],
)


def _handle_service_error(e: InterfaceConfigError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/", response_model=AgentWebappInterfaceConfigPublic)
def get_webapp_interface_config(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Get webapp interface configuration for an agent. Creates default if none exists."""
    try:
        return AgentWebappInterfaceConfigService.get_or_create(
            session, current_user.id, agent_id
        )
    except InterfaceConfigError as e:
        _handle_service_error(e)


@router.put("/", response_model=AgentWebappInterfaceConfigPublic)
def update_webapp_interface_config(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    config_in: AgentWebappInterfaceConfigUpdate,
) -> Any:
    """Update webapp interface configuration for an agent."""
    try:
        return AgentWebappInterfaceConfigService.update(
            session, current_user.id, agent_id, config_in
        )
    except InterfaceConfigError as e:
        _handle_service_error(e)
