import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Message,
    AgenticTeamCreate,
    AgenticTeamUpdate,
    AgenticTeamPublic,
    AgenticTeamsPublic,
    AgenticTeamNodeCreate,
    AgenticTeamNodeUpdate,
    AgenticTeamNodePublic,
    AgenticTeamNodesPublic,
    AgenticTeamNodePositionUpdate,
    AgenticTeamConnectionCreate,
    AgenticTeamConnectionUpdate,
    AgenticTeamConnectionPublic,
    AgenticTeamConnectionsPublic,
    AgenticTeamChartPublic,
    GenerateConnectionPromptResponse,
)
from app.services.agentic_team_service import AgenticTeamService
from app.services.agentic_team_node_service import AgenticTeamNodeService, TeamNodeError
from app.services.agentic_team_connection_service import (
    AgenticTeamConnectionService,
    TeamConnectionError,
)

router = APIRouter(prefix="/agentic-teams", tags=["agentic-teams"])


def _handle_service_error(e: TeamNodeError | TeamConnectionError) -> None:
    """Convert domain exceptions from node/connection services to HTTP responses."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


# ---------------------------------------------------------------------------
# AgenticTeam CRUD
# ---------------------------------------------------------------------------

@router.get("/", response_model=AgenticTeamsPublic)
def list_agentic_teams(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List all agentic teams owned by the current user."""
    teams, count = AgenticTeamService.list_teams(
        session=session, user_id=current_user.id, skip=skip, limit=limit
    )
    return AgenticTeamsPublic(data=teams, count=count)


@router.post("/", response_model=AgenticTeamPublic)
def create_agentic_team(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_in: AgenticTeamCreate,
) -> Any:
    """Create a new agentic team."""
    team = AgenticTeamService.create_team(
        session=session, user_id=current_user.id, data=team_in
    )
    return team


@router.get("/{team_id}", response_model=AgenticTeamPublic)
def get_agentic_team(
    team_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Get a single agentic team by ID."""
    return AgenticTeamService.get_team(
        session=session, team_id=team_id, user_id=current_user.id
    )


@router.put("/{team_id}", response_model=AgenticTeamPublic)
def update_agentic_team(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    team_in: AgenticTeamUpdate,
) -> Any:
    """Update an agentic team's name or icon."""
    return AgenticTeamService.update_team(
        session=session, team_id=team_id, user_id=current_user.id, data=team_in
    )


@router.delete("/{team_id}")
def delete_agentic_team(
    team_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete an agentic team (cascades to nodes and connections)."""
    AgenticTeamService.delete_team(
        session=session, team_id=team_id, user_id=current_user.id
    )
    return Message(message="Agentic team deleted")


# ---------------------------------------------------------------------------
# Chart bulk endpoint
# ---------------------------------------------------------------------------

@router.get("/{team_id}/chart", response_model=AgenticTeamChartPublic)
def get_agentic_team_chart(
    team_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Return team, nodes, and connections in a single request.
    Primary fetch for the chart page — one round-trip instead of three.
    """
    team = AgenticTeamService.get_team(
        session=session, team_id=team_id, user_id=current_user.id
    )
    nodes = AgenticTeamNodeService.list_nodes(
        session=session, team_id=team_id, user_id=current_user.id
    )
    connections = AgenticTeamConnectionService.list_connections(
        session=session, team_id=team_id, user_id=current_user.id
    )

    node_publics = [
        AgenticTeamNodeService.node_to_public(session=session, node=n) for n in nodes
    ]
    conn_publics = [
        AgenticTeamConnectionService.connection_to_public(session=session, conn=c)
        for c in connections
    ]

    return AgenticTeamChartPublic(
        team=AgenticTeamPublic.model_validate(team),
        nodes=node_publics,
        connections=conn_publics,
    )


# ---------------------------------------------------------------------------
# Node endpoints
# IMPORTANT: bulk positions endpoint registered BEFORE {node_id} routes
# to prevent FastAPI treating "positions" as a node_id path param.
# ---------------------------------------------------------------------------

@router.put("/{team_id}/nodes/positions", response_model=list[AgenticTeamNodePublic])
def bulk_update_node_positions(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    positions: list[AgenticTeamNodePositionUpdate],
) -> Any:
    """
    Bulk update node positions. Used after drag-reposition or auto-arrange
    to persist the entire layout in one call.
    """
    try:
        updated_nodes = AgenticTeamNodeService.bulk_update_positions(
            session=session, team_id=team_id, user_id=current_user.id, positions=positions
        )
    except TeamNodeError as e:
        _handle_service_error(e)
    return [
        AgenticTeamNodeService.node_to_public(session=session, node=n)
        for n in updated_nodes
    ]


@router.get("/{team_id}/nodes/", response_model=AgenticTeamNodesPublic)
def list_team_nodes(
    team_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List all nodes for an agentic team."""
    nodes = AgenticTeamNodeService.list_nodes(
        session=session, team_id=team_id, user_id=current_user.id
    )
    node_publics = [
        AgenticTeamNodeService.node_to_public(session=session, node=n) for n in nodes
    ]
    return AgenticTeamNodesPublic(data=node_publics, count=len(node_publics))


@router.post("/{team_id}/nodes/", response_model=AgenticTeamNodePublic)
def create_team_node(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    node_in: AgenticTeamNodeCreate,
) -> Any:
    """Add an agent node to a team."""
    try:
        node = AgenticTeamNodeService.create_node(
            session=session, team_id=team_id, user_id=current_user.id, data=node_in
        )
    except TeamNodeError as e:
        _handle_service_error(e)
    return AgenticTeamNodeService.node_to_public(session=session, node=node)


@router.get("/{team_id}/nodes/{node_id}", response_model=AgenticTeamNodePublic)
def get_team_node(
    team_id: uuid.UUID,
    node_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Get a single node by ID."""
    try:
        node = AgenticTeamNodeService.get_node(
            session=session, team_id=team_id, node_id=node_id, user_id=current_user.id
        )
    except TeamNodeError as e:
        _handle_service_error(e)
    return AgenticTeamNodeService.node_to_public(session=session, node=node)


@router.put("/{team_id}/nodes/{node_id}", response_model=AgenticTeamNodePublic)
def update_team_node(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    node_id: uuid.UUID,
    node_in: AgenticTeamNodeUpdate,
) -> Any:
    """Update a node (is_lead, pos_x, pos_y only)."""
    try:
        node = AgenticTeamNodeService.update_node(
            session=session,
            team_id=team_id,
            node_id=node_id,
            user_id=current_user.id,
            data=node_in,
        )
    except TeamNodeError as e:
        _handle_service_error(e)
    return AgenticTeamNodeService.node_to_public(session=session, node=node)


@router.delete("/{team_id}/nodes/{node_id}")
def delete_team_node(
    team_id: uuid.UUID,
    node_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete a node (cascades to its connections)."""
    try:
        AgenticTeamNodeService.delete_node(
            session=session, team_id=team_id, node_id=node_id, user_id=current_user.id
        )
    except TeamNodeError as e:
        _handle_service_error(e)
    return Message(message="Node deleted")


# ---------------------------------------------------------------------------
# Connection endpoints
# ---------------------------------------------------------------------------

@router.get("/{team_id}/connections/", response_model=AgenticTeamConnectionsPublic)
def list_team_connections(
    team_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List all connections for an agentic team."""
    connections = AgenticTeamConnectionService.list_connections(
        session=session, team_id=team_id, user_id=current_user.id
    )
    conn_publics = [
        AgenticTeamConnectionService.connection_to_public(session=session, conn=c)
        for c in connections
    ]
    return AgenticTeamConnectionsPublic(data=conn_publics, count=len(conn_publics))


@router.post("/{team_id}/connections/", response_model=AgenticTeamConnectionPublic)
def create_team_connection(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    conn_in: AgenticTeamConnectionCreate,
) -> Any:
    """Create a directed connection between two nodes."""
    try:
        conn = AgenticTeamConnectionService.create_connection(
            session=session, team_id=team_id, user_id=current_user.id, data=conn_in
        )
    except TeamConnectionError as e:
        _handle_service_error(e)
    return AgenticTeamConnectionService.connection_to_public(session=session, conn=conn)


@router.get(
    "/{team_id}/connections/{conn_id}", response_model=AgenticTeamConnectionPublic
)
def get_team_connection(
    team_id: uuid.UUID,
    conn_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Get a single connection by ID."""
    try:
        conn = AgenticTeamConnectionService.get_connection(
            session=session, team_id=team_id, conn_id=conn_id, user_id=current_user.id
        )
    except TeamConnectionError as e:
        _handle_service_error(e)
    return AgenticTeamConnectionService.connection_to_public(session=session, conn=conn)


@router.put(
    "/{team_id}/connections/{conn_id}", response_model=AgenticTeamConnectionPublic
)
def update_team_connection(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    team_id: uuid.UUID,
    conn_id: uuid.UUID,
    conn_in: AgenticTeamConnectionUpdate,
) -> Any:
    """Update connection prompt or enabled status."""
    try:
        conn = AgenticTeamConnectionService.update_connection(
            session=session,
            team_id=team_id,
            conn_id=conn_id,
            user_id=current_user.id,
            data=conn_in,
        )
    except TeamConnectionError as e:
        _handle_service_error(e)
    return AgenticTeamConnectionService.connection_to_public(session=session, conn=conn)


@router.delete("/{team_id}/connections/{conn_id}")
def delete_team_connection(
    team_id: uuid.UUID,
    conn_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete a connection."""
    try:
        AgenticTeamConnectionService.delete_connection(
            session=session, team_id=team_id, conn_id=conn_id, user_id=current_user.id
        )
    except TeamConnectionError as e:
        _handle_service_error(e)
    return Message(message="Connection deleted")


@router.post(
    "/{team_id}/connections/{conn_id}/generate-prompt",
    response_model=GenerateConnectionPromptResponse,
)
def generate_connection_prompt(
    team_id: uuid.UUID,
    conn_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Generate a connection handover prompt using AI based on both agents' configurations."""
    try:
        prompt = AgenticTeamConnectionService.generate_connection_prompt(
            session=session,
            team_id=team_id,
            conn_id=conn_id,
            user_id=current_user.id,
        )
    except TeamConnectionError as e:
        _handle_service_error(e)
    return GenerateConnectionPromptResponse(success=True, connection_prompt=prompt)
