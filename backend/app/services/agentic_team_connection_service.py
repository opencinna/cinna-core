from uuid import UUID
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.agentic_team import (
    AgenticTeamNode,
    AgenticTeamConnection,
    AgenticTeamConnectionCreate,
    AgenticTeamConnectionUpdate,
    AgenticTeamConnectionPublic,
)
from app.services.agentic_team_service import AgenticTeamService
from app.services.handover_connection_sync_service import HandoverConnectionSyncService


class TeamConnectionError(Exception):
    """Base exception for team connection service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ConnectionNotFoundError(TeamConnectionError):
    def __init__(self, message: str = "Connection not found"):
        super().__init__(message, status_code=404)


class NodeNotFoundError(TeamConnectionError):
    def __init__(self, message: str = "Node not found"):
        super().__init__(message, status_code=404)


class SelfConnectionError(TeamConnectionError):
    def __init__(self, message: str = "Source and target nodes must be different"):
        super().__init__(message, status_code=400)


class DuplicateConnectionError(TeamConnectionError):
    def __init__(self, message: str = "A connection between these nodes already exists"):
        super().__init__(message, status_code=409)


class AgenticTeamConnectionService:

    @staticmethod
    def get_connection(
        session: Session, team_id: UUID, conn_id: UUID, user_id: UUID
    ) -> AgenticTeamConnection:
        """
        Verify team ownership then verify the connection belongs to the team.
        Raises 404 for any failure condition.
        """
        AgenticTeamService.get_team(session, team_id, user_id)
        conn = session.get(AgenticTeamConnection, conn_id)
        if not conn or conn.team_id != team_id:
            raise ConnectionNotFoundError()
        return conn

    @staticmethod
    def list_connections(
        session: Session, team_id: UUID, user_id: UUID
    ) -> list[AgenticTeamConnection]:
        AgenticTeamService.get_team(session, team_id, user_id)
        statement = select(AgenticTeamConnection).where(
            AgenticTeamConnection.team_id == team_id
        )
        return list(session.exec(statement).all())

    @staticmethod
    def create_connection(
        session: Session,
        team_id: UUID,
        user_id: UUID,
        data: AgenticTeamConnectionCreate,
    ) -> AgenticTeamConnection:
        # 1. Verify team ownership
        AgenticTeamService.get_team(session, team_id, user_id)

        # 2. Prevent self-connection
        if data.source_node_id == data.target_node_id:
            raise SelfConnectionError()

        # 3. Verify source node belongs to this team
        source_node = session.get(AgenticTeamNode, data.source_node_id)
        if not source_node or source_node.team_id != team_id:
            raise NodeNotFoundError("Source node not found")

        # 4. Verify target node belongs to this team
        target_node = session.get(AgenticTeamNode, data.target_node_id)
        if not target_node or target_node.team_id != team_id:
            raise NodeNotFoundError("Target node not found")

        # 5. Prevent duplicate connection
        duplicate_stmt = select(AgenticTeamConnection).where(
            AgenticTeamConnection.team_id == team_id,
            AgenticTeamConnection.source_node_id == data.source_node_id,
            AgenticTeamConnection.target_node_id == data.target_node_id,
        )
        if session.exec(duplicate_stmt).first():
            raise DuplicateConnectionError()

        # 6. Create
        conn = AgenticTeamConnection(
            team_id=team_id,
            source_node_id=data.source_node_id,
            target_node_id=data.target_node_id,
            connection_prompt=data.connection_prompt,
            enabled=data.enabled,
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)

        # 7. Sync AgentHandoverConfig
        HandoverConnectionSyncService.sync_handover_for_connection_created(
            session, source_node, target_node, data.connection_prompt, enabled=data.enabled
        )

        return conn

    @staticmethod
    def update_connection(
        session: Session,
        team_id: UUID,
        conn_id: UUID,
        user_id: UUID,
        data: AgenticTeamConnectionUpdate,
    ) -> AgenticTeamConnection:
        conn = AgenticTeamConnectionService.get_connection(
            session, team_id, conn_id, user_id
        )
        update_dict = data.model_dump(exclude_unset=True)
        conn.sqlmodel_update(update_dict)
        conn.updated_at = datetime.now(timezone.utc)
        session.add(conn)
        session.commit()
        session.refresh(conn)

        # Sync AgentHandoverConfig
        source_node = session.get(AgenticTeamNode, conn.source_node_id)
        target_node = session.get(AgenticTeamNode, conn.target_node_id)
        if source_node and target_node:
            HandoverConnectionSyncService.sync_handover_for_connection_updated(
                session, source_node, target_node, conn, update_dict
            )

        return conn

    @staticmethod
    def delete_connection(
        session: Session, team_id: UUID, conn_id: UUID, user_id: UUID
    ) -> None:
        conn = AgenticTeamConnectionService.get_connection(
            session, team_id, conn_id, user_id
        )

        # Capture agent IDs before deletion for handover cleanup
        source_node = session.get(AgenticTeamNode, conn.source_node_id)
        target_node = session.get(AgenticTeamNode, conn.target_node_id)

        session.delete(conn)
        session.commit()

        # Remove handover config if no other connections exist between these agents
        if source_node and target_node:
            HandoverConnectionSyncService.sync_handover_for_connection_deleted(
                session, source_node, target_node
            )

    @staticmethod
    def connection_to_public(
        session: Session, conn: AgenticTeamConnection
    ) -> AgenticTeamConnectionPublic:
        """Resolve source/target node names, color presets, and build the public response model."""
        from app.models.agent import Agent

        source_node = session.get(AgenticTeamNode, conn.source_node_id)
        target_node = session.get(AgenticTeamNode, conn.target_node_id)
        source_name = source_node.name if source_node else ""
        target_name = target_node.name if target_node else ""

        source_color = None
        target_color = None
        if source_node:
            source_agent = session.get(Agent, source_node.agent_id)
            source_color = source_agent.ui_color_preset if source_agent else None
        if target_node:
            target_agent = session.get(Agent, target_node.agent_id)
            target_color = target_agent.ui_color_preset if target_agent else None

        return AgenticTeamConnectionPublic(
            id=conn.id,
            team_id=conn.team_id,
            source_node_id=conn.source_node_id,
            target_node_id=conn.target_node_id,
            source_node_name=source_name,
            target_node_name=target_name,
            source_node_color_preset=source_color,
            target_node_color_preset=target_color,
            connection_prompt=conn.connection_prompt,
            enabled=conn.enabled,
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    @staticmethod
    def generate_connection_prompt(
        session: Session,
        team_id: UUID,
        conn_id: UUID,
        user_id: UUID,
    ) -> str:
        """Generate a connection prompt using AI, based on the source and target agents.

        Returns the generated prompt string.
        Raises TeamConnectionError on failure.

        Inline imports avoid circular dependencies between model modules.
        """
        from app.models.agent import Agent
        from app.models.user import User
        from app.services.ai_functions_service import AIFunctionsService

        conn = AgenticTeamConnectionService.get_connection(session, team_id, conn_id, user_id)

        source_node = session.get(AgenticTeamNode, conn.source_node_id)
        target_node = session.get(AgenticTeamNode, conn.target_node_id)
        if not source_node or not target_node:
            raise TeamConnectionError("Source or target node not found", status_code=404)

        source_agent = session.get(Agent, source_node.agent_id)
        target_agent = session.get(Agent, target_node.agent_id)
        if not source_agent or not target_agent:
            raise TeamConnectionError("Source or target agent not found", status_code=404)

        user = session.get(User, user_id)

        result = AIFunctionsService.generate_handover_prompt(
            source_agent_name=source_agent.name,
            source_entrypoint=source_agent.entrypoint_prompt,
            source_workflow=source_agent.workflow_prompt,
            target_agent_name=target_agent.name,
            target_entrypoint=target_agent.entrypoint_prompt,
            target_workflow=target_agent.workflow_prompt,
            user=user,
            db=session,
        )

        if not result.get("success"):
            raise TeamConnectionError(
                result.get("error", "Failed to generate prompt"), status_code=500
            )
        return result["handover_prompt"]
