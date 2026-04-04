"""
Handover-Connection Sync Service — owns ALL bidirectional sync logic between
AgentHandoverConfig and AgenticTeamConnection.

When a handover is created/deleted, team connections must be kept in sync,
and vice versa. This service is the single place that understands both sides.
"""
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.agents.agent_handover import AgentHandoverConfig
from app.models.agentic_teams.agentic_team import AgenticTeamNode, AgenticTeamConnection


class HandoverConnectionSyncService:
    """Keeps AgentHandoverConfig and AgenticTeamConnection in sync."""

    # ------------------------------------------------------------------
    # Called by AgentHandoverService (handover → connections)
    # ------------------------------------------------------------------

    @staticmethod
    def sync_connections_for_handover_created(
        session: Session,
        source_agent_id: uuid.UUID,
        target_agent_id: uuid.UUID,
        connection_prompt: str,
        *,
        enabled: bool,
    ) -> None:
        """Create team connections for all teams where both agents are nodes.

        Only creates connections that do not already exist.
        """
        source_nodes = session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.agent_id == source_agent_id
            )
        ).all()
        target_nodes = session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.agent_id == target_agent_id
            )
        ).all()

        target_by_team = {n.team_id: n for n in target_nodes}

        connections_to_add = []
        for source_node in source_nodes:
            target_node = target_by_team.get(source_node.team_id)
            if target_node is None:
                continue

            existing = session.exec(
                select(AgenticTeamConnection).where(
                    AgenticTeamConnection.source_node_id == source_node.id,
                    AgenticTeamConnection.target_node_id == target_node.id,
                )
            ).first()
            if existing is None:
                connections_to_add.append(
                    AgenticTeamConnection(
                        team_id=source_node.team_id,
                        source_node_id=source_node.id,
                        target_node_id=target_node.id,
                        connection_prompt=connection_prompt,
                        enabled=enabled,
                    )
                )

        if connections_to_add:
            for conn in connections_to_add:
                session.add(conn)
            session.commit()

    @staticmethod
    def sync_connections_for_handover_deleted(
        session: Session,
        source_agent_id: uuid.UUID,
        target_agent_id: uuid.UUID,
    ) -> None:
        """Delete team connections that correspond to a deleted handover config."""
        source_nodes = session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.agent_id == source_agent_id
            )
        ).all()
        target_nodes = session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.agent_id == target_agent_id
            )
        ).all()

        target_by_team = {n.team_id: n for n in target_nodes}

        deleted_any = False
        for source_node in source_nodes:
            target_node = target_by_team.get(source_node.team_id)
            if target_node is None:
                continue

            conn = session.exec(
                select(AgenticTeamConnection).where(
                    AgenticTeamConnection.source_node_id == source_node.id,
                    AgenticTeamConnection.target_node_id == target_node.id,
                )
            ).first()
            if conn is not None:
                session.delete(conn)
                deleted_any = True

        if deleted_any:
            session.commit()

    # ------------------------------------------------------------------
    # Called by AgenticTeamConnectionService (connection → handover)
    # ------------------------------------------------------------------

    @staticmethod
    def sync_handover_for_connection_created(
        session: Session,
        source_node: AgenticTeamNode,
        target_node: AgenticTeamNode,
        connection_prompt: str,
        *,
        enabled: bool,
    ) -> None:
        """Create or update AgentHandoverConfig when a connection is created."""
        handover = session.exec(
            select(AgentHandoverConfig).where(
                AgentHandoverConfig.source_agent_id == source_node.agent_id,
                AgentHandoverConfig.target_agent_id == target_node.agent_id,
            )
        ).first()
        if handover is None:
            handover = AgentHandoverConfig(
                source_agent_id=source_node.agent_id,
                target_agent_id=target_node.agent_id,
                handover_prompt=connection_prompt,
                enabled=enabled,
            )
            session.add(handover)
        else:
            handover.handover_prompt = connection_prompt
            handover.enabled = enabled
            handover.updated_at = datetime.now(timezone.utc)
            session.add(handover)
        session.commit()

    @staticmethod
    def sync_handover_for_connection_updated(
        session: Session,
        source_node: AgenticTeamNode,
        target_node: AgenticTeamNode,
        conn: AgenticTeamConnection,
        update_dict: dict,
    ) -> None:
        """Update or create AgentHandoverConfig when a connection is updated."""
        handover = session.exec(
            select(AgentHandoverConfig).where(
                AgentHandoverConfig.source_agent_id == source_node.agent_id,
                AgentHandoverConfig.target_agent_id == target_node.agent_id,
            )
        ).first()
        if handover is None:
            handover = AgentHandoverConfig(
                source_agent_id=source_node.agent_id,
                target_agent_id=target_node.agent_id,
                handover_prompt=conn.connection_prompt,
                enabled=conn.enabled,
            )
            session.add(handover)
        else:
            if "connection_prompt" in update_dict:
                handover.handover_prompt = conn.connection_prompt
            if "enabled" in update_dict:
                handover.enabled = conn.enabled
            handover.updated_at = datetime.now(timezone.utc)
            session.add(handover)
        session.commit()

    @staticmethod
    def sync_handover_for_connection_deleted(
        session: Session,
        source_node: AgenticTeamNode,
        target_node: AgenticTeamNode,
    ) -> None:
        """Remove handover config if no other connections exist between these agents."""
        source_node_ids = [
            n.id
            for n in session.exec(
                select(AgenticTeamNode).where(
                    AgenticTeamNode.agent_id == source_node.agent_id
                )
            ).all()
        ]
        target_node_ids = [
            n.id
            for n in session.exec(
                select(AgenticTeamNode).where(
                    AgenticTeamNode.agent_id == target_node.agent_id
                )
            ).all()
        ]
        other_conn = session.exec(
            select(AgenticTeamConnection).where(
                AgenticTeamConnection.source_node_id.in_(source_node_ids),
                AgenticTeamConnection.target_node_id.in_(target_node_ids),
            )
        ).first()
        if other_conn is None:
            handover = session.exec(
                select(AgentHandoverConfig).where(
                    AgentHandoverConfig.source_agent_id == source_node.agent_id,
                    AgentHandoverConfig.target_agent_id == target_node.agent_id,
                )
            ).first()
            if handover is not None:
                session.delete(handover)
                session.commit()

    # ------------------------------------------------------------------
    # Called by AgenticTeamNodeService (node added → auto-create connections)
    # ------------------------------------------------------------------

    @staticmethod
    def sync_connections_for_node_added(
        session: Session,
        new_node: AgenticTeamNode,
        team_id: uuid.UUID,
    ) -> None:
        """Create connections from existing handover configs when a node is added.

        For each other node already in the team, check both directions for a
        matching AgentHandoverConfig and create the corresponding connection if
        it does not already exist.
        """
        other_nodes = session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.team_id == team_id,
                AgenticTeamNode.id != new_node.id,
            )
        ).all()

        connections_to_add = []
        for other_node in other_nodes:
            # Check new_node → other_node direction
            handover_fwd = session.exec(
                select(AgentHandoverConfig).where(
                    AgentHandoverConfig.source_agent_id == new_node.agent_id,
                    AgentHandoverConfig.target_agent_id == other_node.agent_id,
                )
            ).first()
            if handover_fwd is not None:
                existing = session.exec(
                    select(AgenticTeamConnection).where(
                        AgenticTeamConnection.source_node_id == new_node.id,
                        AgenticTeamConnection.target_node_id == other_node.id,
                    )
                ).first()
                if existing is None:
                    connections_to_add.append(
                        AgenticTeamConnection(
                            team_id=team_id,
                            source_node_id=new_node.id,
                            target_node_id=other_node.id,
                            connection_prompt=handover_fwd.handover_prompt,
                            enabled=handover_fwd.enabled,
                        )
                    )

            # Check other_node → new_node direction
            handover_rev = session.exec(
                select(AgentHandoverConfig).where(
                    AgentHandoverConfig.source_agent_id == other_node.agent_id,
                    AgentHandoverConfig.target_agent_id == new_node.agent_id,
                )
            ).first()
            if handover_rev is not None:
                existing = session.exec(
                    select(AgenticTeamConnection).where(
                        AgenticTeamConnection.source_node_id == other_node.id,
                        AgenticTeamConnection.target_node_id == new_node.id,
                    )
                ).first()
                if existing is None:
                    connections_to_add.append(
                        AgenticTeamConnection(
                            team_id=team_id,
                            source_node_id=other_node.id,
                            target_node_id=new_node.id,
                            connection_prompt=handover_rev.handover_prompt,
                            enabled=handover_rev.enabled,
                        )
                    )

        if connections_to_add:
            for conn in connections_to_add:
                session.add(conn)
            session.commit()
