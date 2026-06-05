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
from app.models.mcp.mcp_token import (
    MCPConnectorTokenCreate,
    MCPConnectorTokenUpdate,
    MCPConnectorTokenPublic,
    MCPConnectorTokenCreated,
    MCPConnectorTokensPublic,
)
from app.core.config import settings
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.mcp.mcp_direct_token_service import MCPDirectTokenService
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
    return MCPConnectorService.to_public(connector, db_session=session)


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
        data=[MCPConnectorService.to_public(c, db_session=session) for c in connectors],
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
    return MCPConnectorService.to_public(connector, db_session=session)


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
    return MCPConnectorService.to_public(connector, db_session=session)


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


# ---------------------------------------------------------------------------
# Direct access tokens (connector-scoped opaque bearer tokens)
# ---------------------------------------------------------------------------


def _load_owned_connector(session, agent_id: uuid.UUID, connector_id: uuid.UUID, user_id: uuid.UUID):
    """Verify agent ownership and load the connector belonging to that agent."""
    _check_agent_owner(session, agent_id, user_id)
    connector = MCPConnectorService.get_connector(
        db_session=session, connector_id=connector_id
    )
    if not connector or connector.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


@router.get(
    "/{agent_id}/mcp-connectors/{connector_id}/tokens",
    response_model=MCPConnectorTokensPublic,
)
def list_connector_tokens(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> Any:
    """List direct access tokens for a connector (owner only). Never leaks the token value."""
    _load_owned_connector(session, agent_id, connector_id, current_user.id)
    tokens = MCPDirectTokenService.list_tokens(
        db_session=session, connector_id=connector_id
    )
    return MCPConnectorTokensPublic(data=tokens, count=len(tokens))


@router.post(
    "/{agent_id}/mcp-connectors/{connector_id}/tokens",
    response_model=MCPConnectorTokenCreated,
)
def create_connector_token(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
    token_in: MCPConnectorTokenCreate,
) -> Any:
    """Mint a direct access token (owner only, requires allow_token_access).

    Returns the full token value exactly once.
    """
    connector = _load_owned_connector(session, agent_id, connector_id, current_user.id)
    if not connector.allow_token_access:
        raise HTTPException(
            status_code=403,
            detail="Direct token access is disabled for this connector",
        )
    return MCPDirectTokenService.create_token(
        db_session=session, connector=connector, label=token_in.label
    )


@router.put(
    "/{agent_id}/mcp-connectors/{connector_id}/tokens/{token_id}",
    response_model=MCPConnectorTokenPublic,
)
def update_connector_token(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
    token_id: uuid.UUID,
    token_in: MCPConnectorTokenUpdate,
) -> Any:
    """Revoke or restore a direct access token (owner only)."""
    _load_owned_connector(session, agent_id, connector_id, current_user.id)
    token = MCPDirectTokenService.get_token(
        db_session=session, token_id=token_id, connector_id=connector_id
    )
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    if token_in.revoked is None:
        return MCPDirectTokenService.to_public(token)
    return MCPDirectTokenService.set_revoked(
        db_session=session, token=token, revoked=token_in.revoked
    )


@router.delete("/{agent_id}/mcp-connectors/{connector_id}/tokens/{token_id}")
def delete_connector_token(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    connector_id: uuid.UUID,
    token_id: uuid.UUID,
) -> Message:
    """Delete a direct access token (owner only)."""
    _load_owned_connector(session, agent_id, connector_id, current_user.id)
    token = MCPDirectTokenService.get_token(
        db_session=session, token_id=token_id, connector_id=connector_id
    )
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    MCPDirectTokenService.delete_token(db_session=session, token=token)
    return Message(message="Token deleted successfully")
