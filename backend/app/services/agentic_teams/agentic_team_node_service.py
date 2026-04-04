from uuid import UUID
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.agentic_teams.agentic_team import (
    AgenticTeamNode,
    AgenticTeamNodeCreate,
    AgenticTeamNodeUpdate,
    AgenticTeamNodePublic,
    AgenticTeamNodePositionUpdate,
)
from app.services.agentic_teams.agentic_team_service import AgenticTeamService
from app.services.agents.handover_connection_sync_service import HandoverConnectionSyncService


class TeamNodeError(Exception):
    """Base exception for team node service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NodeNotFoundError(TeamNodeError):
    def __init__(self, message: str = "Node not found"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(TeamNodeError):
    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class DuplicateNodeError(TeamNodeError):
    def __init__(self, message: str = "This agent is already in the team"):
        super().__init__(message, status_code=409)


class InvalidNodeError(TeamNodeError):
    def __init__(self, message: str = "One or more node IDs do not belong to this team"):
        super().__init__(message, status_code=400)


class AgenticTeamNodeService:

    @staticmethod
    def get_node(
        session: Session, team_id: UUID, node_id: UUID, user_id: UUID
    ) -> AgenticTeamNode:
        """
        Verify team ownership then verify the node belongs to the team.
        Raises 404 for any failure condition.
        """
        AgenticTeamService.get_team(session, team_id, user_id)  # raises 404 if bad
        node = session.get(AgenticTeamNode, node_id)
        if not node or node.team_id != team_id:
            raise NodeNotFoundError()
        return node

    @staticmethod
    def list_nodes(
        session: Session, team_id: UUID, user_id: UUID
    ) -> list[AgenticTeamNode]:
        AgenticTeamService.get_team(session, team_id, user_id)
        statement = select(AgenticTeamNode).where(AgenticTeamNode.team_id == team_id)
        return list(session.exec(statement).all())

    @staticmethod
    def create_node(
        session: Session, team_id: UUID, user_id: UUID, data: AgenticTeamNodeCreate
    ) -> AgenticTeamNode:
        # 1. Verify team ownership
        AgenticTeamService.get_team(session, team_id, user_id)

        # 2. Verify agent ownership
        agent = session.get(Agent, data.agent_id)
        if not agent or agent.owner_id != user_id:
            raise AgentNotFoundError()

        # 3. Prevent duplicate agent in same team
        duplicate_stmt = select(AgenticTeamNode).where(
            AgenticTeamNode.team_id == team_id,
            AgenticTeamNode.agent_id == data.agent_id,
        )
        if session.exec(duplicate_stmt).first():
            raise DuplicateNodeError()

        # 4. If setting as lead: unmark any existing lead in this team
        if data.is_lead:
            existing_lead_stmt = select(AgenticTeamNode).where(
                AgenticTeamNode.team_id == team_id,
                AgenticTeamNode.is_lead == True,  # noqa: E712
            )
            existing_lead = session.exec(existing_lead_stmt).first()
            if existing_lead:
                existing_lead.is_lead = False
                session.add(existing_lead)

        # 5. Create the node (name auto-populated from agent)
        node = AgenticTeamNode(
            team_id=team_id,
            name=agent.name,
            node_type="agent",
            is_lead=data.is_lead,
            agent_id=data.agent_id,
            pos_x=data.pos_x,
            pos_y=data.pos_y,
        )
        session.add(node)
        session.commit()
        session.refresh(node)

        # 6. Auto-create connections from existing handover configs
        HandoverConnectionSyncService.sync_connections_for_node_added(session, node, team_id)

        return node

    @staticmethod
    def update_node(
        session: Session,
        team_id: UUID,
        node_id: UUID,
        user_id: UUID,
        data: AgenticTeamNodeUpdate,
    ) -> AgenticTeamNode:
        node = AgenticTeamNodeService.get_node(session, team_id, node_id, user_id)

        # If setting as lead: unmark any other existing lead in this team
        if data.is_lead is True:
            existing_lead_stmt = select(AgenticTeamNode).where(
                AgenticTeamNode.team_id == team_id,
                AgenticTeamNode.is_lead == True,  # noqa: E712
                AgenticTeamNode.id != node_id,
            )
            existing_lead = session.exec(existing_lead_stmt).first()
            if existing_lead:
                existing_lead.is_lead = False
                session.add(existing_lead)

        # Apply only the provided fields
        update_dict = data.model_dump(exclude_unset=True)
        node.sqlmodel_update(update_dict)
        node.updated_at = datetime.now(timezone.utc)

        session.add(node)
        session.commit()
        session.refresh(node)
        return node

    @staticmethod
    def delete_node(
        session: Session, team_id: UUID, node_id: UUID, user_id: UUID
    ) -> None:
        node = AgenticTeamNodeService.get_node(session, team_id, node_id, user_id)
        session.delete(node)
        session.commit()

    @staticmethod
    def bulk_update_positions(
        session: Session,
        team_id: UUID,
        user_id: UUID,
        positions: list[AgenticTeamNodePositionUpdate],
    ) -> list[AgenticTeamNode]:
        # 1. Verify team ownership
        AgenticTeamService.get_team(session, team_id, user_id)

        # 2. Fetch all current nodes for the team
        statement = select(AgenticTeamNode).where(AgenticTeamNode.team_id == team_id)
        team_nodes = list(session.exec(statement).all())
        valid_ids = {node.id for node in team_nodes}

        # 3. Validate all IDs belong to this team
        for pos in positions:
            if pos.id not in valid_ids:
                raise InvalidNodeError()

        # 4. Build lookup and update positions
        node_map = {node.id: node for node in team_nodes}
        now = datetime.now(timezone.utc)
        updated_nodes = []
        for pos in positions:
            node = node_map[pos.id]
            node.pos_x = pos.pos_x
            node.pos_y = pos.pos_y
            node.updated_at = now
            session.add(node)
            updated_nodes.append(node)

        session.commit()
        for node in updated_nodes:
            session.refresh(node)
        return updated_nodes

    @staticmethod
    def node_to_public(
        session: Session, node: AgenticTeamNode
    ) -> AgenticTeamNodePublic:
        """Resolve agent_ui_color_preset and build the public response model."""
        agent = session.get(Agent, node.agent_id)
        color = agent.ui_color_preset if agent else None
        return AgenticTeamNodePublic(
            id=node.id,
            team_id=node.team_id,
            agent_id=node.agent_id,
            name=node.name,
            agent_ui_color_preset=color,
            node_type=node.node_type,
            is_lead=node.is_lead,
            pos_x=node.pos_x,
            pos_y=node.pos_y,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )
