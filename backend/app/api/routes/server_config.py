"""
Server configuration API.

Exposes the singleton server-wide config. The disclaimer projection is readable
by any authenticated user; the full config is superuser-only (read + update).
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.models import User
from app.models.server_config.server_config import (
    DisclaimerPublic,
    ServerConfig,
    ServerConfigUpdate,
)
from app.services.server_config.server_config_service import ServerConfigService

router = APIRouter(tags=["server-config"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/server-config/disclaimer", response_model=DisclaimerPublic)
def get_disclaimer(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Return the disclaimer projection for any authenticated user."""
    config = ServerConfigService.get_or_create(session)
    return ServerConfigService.to_disclaimer_public(config)


@router.get("/admin/server-config", response_model=ServerConfig)
def get_server_config(
    session: SessionDep,
    current_user: SuperUser,
) -> Any:
    """Return the full server configuration (superuser only)."""
    return ServerConfigService.get_or_create(session)


@router.put("/admin/server-config", response_model=ServerConfig)
def update_server_config(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: ServerConfigUpdate,
) -> Any:
    """Update the server configuration (superuser only)."""
    return ServerConfigService.update(session, data, current_user.id)
