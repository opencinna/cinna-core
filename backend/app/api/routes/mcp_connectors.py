import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    Message,
)
from app.models.mcp.mcp_connector import (
    MCPConnectorCreate,
    MCPConnectorUpdate,
    MCPConnectorPublic,
    MCPConnectorsPublic,
)
from app.core.config import settings
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.mcp.mcp_errors import MCPError

router = APIRouter(prefix="/agents", tags=["mcp-connectors"])


def _check_agent_owner(session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """Verify agent exists and user is the owner."""
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return agent


@router.post(
    "/{agent_id}/mcp-connectors",
    response_model=MCPConnectorPublic,
)
def create_mcp_connector(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_in: MCPConnectorCreate,
) -> Any:
    """Create a new MCP connector for an agent."""
    _check_agent_owner(session, agent_id, current_user.id)
    connector = MCPConnectorService.create_connector(
        db_session=session,
        agent_id=agent_id,
        owner_id=current_user.id,
        data=connector_in,
    )
    return MCPConnectorService.to_public(connector)


@router.get(
    "/{agent_id}/mcp-connectors",
    response_model=MCPConnectorsPublic,
)
def list_mcp_connectors(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """List all MCP connectors for an agent."""
    _check_agent_owner(session, agent_id, current_user.id)
    connectors = MCPConnectorService.list_connectors(
        db_session=session,
        agent_id=agent_id,
        owner_id=current_user.id,
    )
    return MCPConnectorsPublic(
        data=[MCPConnectorService.to_public(c) for c in connectors],
        count=len(connectors),
        mcp_server_base_url=settings.MCP_SERVER_BASE_URL or None,
    )


@router.get(
    "/{agent_id}/mcp-connectors/{connector_id}",
    response_model=MCPConnectorPublic,
)
def get_mcp_connector(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> Any:
    """Get a specific MCP connector."""
    _check_agent_owner(session, agent_id, current_user.id)
    connector = MCPConnectorService.get_connector(
        db_session=session,
        connector_id=connector_id,
    )
    if not connector or connector.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    return MCPConnectorService.to_public(connector)


@router.put(
    "/{agent_id}/mcp-connectors/{connector_id}",
    response_model=MCPConnectorPublic,
)
def update_mcp_connector(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
    connector_in: MCPConnectorUpdate,
) -> Any:
    """Update an MCP connector."""
    _check_agent_owner(session, agent_id, current_user.id)
    try:
        connector = MCPConnectorService.update_connector(
            db_session=session,
            connector_id=connector_id,
            owner_id=current_user.id,
            data=connector_in,
        )
    except MCPError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return MCPConnectorService.to_public(connector)


@router.delete("/{agent_id}/mcp-connectors/{connector_id}")
def delete_mcp_connector(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> Message:
    """Delete an MCP connector."""
    _check_agent_owner(session, agent_id, current_user.id)
    try:
        deleted = MCPConnectorService.delete_connector(
            db_session=session,
            connector_id=connector_id,
            owner_id=current_user.id,
        )
    except MCPError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connector not found")
    return Message(message="Connector deleted successfully")
