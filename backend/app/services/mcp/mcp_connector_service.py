import uuid
import logging
from datetime import datetime, UTC

from sqlmodel import Session as DBSession, select, func

from app.models.mcp.mcp_connector import MCPConnector, MCPConnectorCreate, MCPConnectorUpdate
from app.models.mcp.mcp_oauth_client import MCPOAuthClient
from app.models import Agent, User
from app.models.environments.environment import AgentEnvironment
from app.core.config import settings
from app.services.mcp.mcp_errors import (
    ConnectorNotFoundError,
    ConnectorInactiveError,
    MCPPermissionDeniedError,
    AgentNotAvailableError,
    EnvironmentNotFoundError,
)

logger = logging.getLogger(__name__)


class MCPConnectorService:
    @staticmethod
    def create_connector(
        db_session: DBSession,
        agent_id: uuid.UUID,
        owner_id: uuid.UUID,
        data: MCPConnectorCreate,
    ) -> MCPConnector:
        connector = MCPConnector(
            agent_id=agent_id,
            owner_id=owner_id,
            name=data.name,
            mode=data.mode,
            is_agent_to_agent=data.is_agent_to_agent,
            allowed_emails=data.allowed_emails,
            allowed_user_ids=[str(uid) for uid in data.allowed_user_ids],
            allow_token_access=data.allow_token_access,
            max_clients=data.max_clients,
        )
        db_session.add(connector)
        db_session.commit()
        db_session.refresh(connector)
        return connector

    @staticmethod
    def list_connectors(
        db_session: DBSession,
        agent_id: uuid.UUID,
        owner_id: uuid.UUID,
    ) -> list[MCPConnector]:
        statement = select(MCPConnector).where(
            MCPConnector.agent_id == agent_id,
            MCPConnector.owner_id == owner_id,
        )
        return list(db_session.exec(statement).all())

    @staticmethod
    def get_connector(
        db_session: DBSession,
        connector_id: uuid.UUID,
    ) -> MCPConnector | None:
        return db_session.get(MCPConnector, connector_id)

    @staticmethod
    def update_connector(
        db_session: DBSession,
        connector_id: uuid.UUID,
        owner_id: uuid.UUID,
        data: MCPConnectorUpdate,
    ) -> MCPConnector | None:
        connector = db_session.get(MCPConnector, connector_id)
        if not connector:
            return None
        if connector.owner_id != owner_id:
            raise MCPPermissionDeniedError()

        update_dict = data.model_dump(exclude_unset=True)
        # Persist UUIDs as strings in the JSON column for consistency.
        if "allowed_user_ids" in update_dict and update_dict["allowed_user_ids"] is not None:
            update_dict["allowed_user_ids"] = [
                str(uid) for uid in update_dict["allowed_user_ids"]
            ]
        connector.sqlmodel_update(update_dict)
        connector.updated_at = datetime.now(UTC)

        db_session.add(connector)
        db_session.commit()
        db_session.refresh(connector)

        # Evict MCP server if deactivated
        if "is_active" in update_dict and not connector.is_active:
            from app.mcp.server import mcp_registry
            mcp_registry.remove(str(connector_id))

        return connector

    @staticmethod
    def delete_connector(
        db_session: DBSession,
        connector_id: uuid.UUID,
        owner_id: uuid.UUID,
    ) -> bool:
        connector = db_session.get(MCPConnector, connector_id)
        if not connector:
            return False
        if connector.owner_id != owner_id:
            raise MCPPermissionDeniedError()

        # Auto-cleanup on disconnect (Fix 4A): delete every agent2agent
        # mcp_provider credential built from this connector BEFORE the connector
        # (and its CASCADE-bound mcp_tokens) is removed, so we can still resolve
        # credential <- bound token <- connector. Manual/external mcp_provider
        # credentials and all other types are left untouched (the helper gate is
        # auth_mode=="agent2agent").
        MCPConnectorService._cleanup_agent2agent_credentials(db_session, connector_id)

        db_session.delete(connector)

        # Evict MCP server from registry
        from app.mcp.server import mcp_registry
        mcp_registry.remove(str(connector_id))

        db_session.commit()
        return True

    @staticmethod
    def _cleanup_agent2agent_credentials(
        db_session: DBSession,
        connector_id: uuid.UUID,
    ) -> None:
        """
        Delete agent2agent ``mcp_provider`` credentials bound to ``connector_id``.

        Resolves the credentials via their bound ``mcp_token`` (whose
        ``connector_id`` points at this connector), gates each on the shared
        agent2agent helper, and deletes via the gate-bypassing internal delete
        (the connector owner is not necessarily the credential owner, and this is
        the intended disconnect path for a dead pair connection). Each delete
        cascade-removes the bound token + ``AgentCredentialLink`` rows and fires
        the consumer env-sync so the dead MCP server drops from ``user_mcp.json``.

        ``delete_connector`` is a sync route handler with no running event loop, so
        the async credential-delete + env-sync is driven via ``asyncio.run``
        (the established sync→async bridge, e.g. ``model_discovery_scheduler``).
        """
        import asyncio

        from app.models.mcp.mcp_token import MCPToken
        from app.models.credentials.credential import Credential
        from app.services.credentials.credentials_service import CredentialsService

        credential_ids = {
            cid
            for cid in db_session.exec(
                select(MCPToken.credential_id).where(
                    MCPToken.connector_id == connector_id,
                    MCPToken.credential_id.is_not(None),
                )
            ).all()
            if cid is not None
        }
        if not credential_ids:
            return

        async def _delete_all() -> None:
            for credential_id in credential_ids:
                credential = db_session.get(Credential, credential_id)
                if credential is None:
                    continue
                if not CredentialsService._is_agent2agent_mcp_provider(
                    db_session, credential
                ):
                    # Defensive: a non-agent2agent credential bound to this
                    # connector should not exist, but never auto-delete one.
                    continue
                await CredentialsService._delete_credential_internal(
                    db_session, credential
                )

        asyncio.run(_delete_all())

    @staticmethod
    def check_user_access(
        db_session: DBSession,
        connector_id: uuid.UUID,
        user: User,
    ) -> bool:
        """Check if a user may access the connector.

        Access is granted when the user is the connector owner, their id is in
        ``allowed_user_ids``, or (legacy fallback) their email is in
        ``allowed_emails``.
        """
        connector = db_session.get(MCPConnector, connector_id)
        if not connector:
            return False
        if connector.owner_id == user.id:
            return True
        if str(user.id) in [str(u) for u in (connector.allowed_user_ids or [])]:
            return True
        if (
            user.email
            and connector.allowed_emails
            and user.email.lower() in [e.lower() for e in connector.allowed_emails]
        ):
            return True
        return False

    @staticmethod
    def check_email_access(
        db_session: DBSession,
        connector_id: uuid.UUID,
        email: str,
    ) -> bool:
        """Legacy email-only ACL check (kept for backward compatibility).

        Prefer ``check_user_access`` which also honours ``allowed_user_ids``.
        """
        connector = db_session.get(MCPConnector, connector_id)
        if not connector:
            return False
        if not connector.allowed_emails:
            return False
        return email.lower() in [e.lower() for e in connector.allowed_emails]

    @staticmethod
    def get_registered_client_count(
        db_session: DBSession,
        connector_id: uuid.UUID,
    ) -> int:
        statement = select(func.count()).where(
            MCPOAuthClient.connector_id == connector_id
        )
        return db_session.exec(statement).one()

    @staticmethod
    def resolve_connector_context(
        db_session: DBSession,
        connector_id: uuid.UUID,
    ) -> tuple[MCPConnector, Agent, AgentEnvironment]:
        """
        Load and validate connector, agent, and environment for a tool request.

        Used by the MCP tool handler to resolve all entities needed before
        delegating to MCPRequestHandler (same pattern as A2A route resolution).

        Args:
            db_session: Database session
            connector_id: MCP connector UUID

        Returns:
            (connector, agent, environment) tuple

        Raises:
            ConnectorNotFoundError: If connector doesn't exist.
            ConnectorInactiveError: If connector is inactive.
            AgentNotAvailableError: If agent is missing or has no environment.
            EnvironmentNotFoundError: If the agent environment doesn't exist.
        """
        connector = db_session.get(MCPConnector, connector_id)
        if not connector:
            raise ConnectorNotFoundError()
        if not connector.is_active:
            raise ConnectorInactiveError()

        agent = db_session.get(Agent, connector.agent_id)
        if not agent or not agent.active_environment_id:
            raise AgentNotAvailableError()

        environment = db_session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise EnvironmentNotFoundError()

        return connector, agent, environment

    @staticmethod
    def _resolve_allowed_users(
        db_session: DBSession,
        allowed_user_ids: list,
    ) -> list[dict]:
        """Batch-resolve allowed_user_ids to display info (id/email/full_name).

        Single query to avoid an N+1 across the connector list. IDs that no
        longer resolve to a user are dropped from the display projection (the
        raw id stays in ``allowed_user_ids`` until the owner removes it).
        """
        if not allowed_user_ids:
            return []
        ids = [str(u) for u in allowed_user_ids]
        users = db_session.exec(select(User).where(User.id.in_(ids))).all()
        return [
            {"id": u.id, "email": u.email, "full_name": u.full_name}
            for u in users
        ]

    @staticmethod
    def to_public(connector: MCPConnector, db_session: DBSession | None = None) -> dict:
        """Convert connector to public dict with computed mcp_server_url.

        When ``db_session`` is provided, ``allowed_users`` is resolved to
        display info for the frontend picker; otherwise it is empty.
        """
        allowed_user_ids = connector.allowed_user_ids or []
        allowed_users = (
            MCPConnectorService._resolve_allowed_users(db_session, allowed_user_ids)
            if db_session is not None
            else []
        )
        data = {
            "id": connector.id,
            "agent_id": connector.agent_id,
            "owner_id": connector.owner_id,
            "name": connector.name,
            "mode": connector.mode,
            "is_active": connector.is_active,
            "is_agent_to_agent": connector.is_agent_to_agent,
            "allowed_emails": connector.allowed_emails or [],
            "allowed_user_ids": allowed_user_ids,
            "allowed_users": allowed_users,
            "allow_token_access": connector.allow_token_access,
            "max_clients": connector.max_clients,
            "mcp_server_url": f"{settings.MCP_SERVER_BASE_URL}/{connector.id}/mcp" if settings.MCP_SERVER_BASE_URL else None,
            "created_at": connector.created_at,
            "updated_at": connector.updated_at,
        }
        return data
