"""
MCP Direct Token Service — connector-scoped opaque bearer tokens.

Direct tokens are MCP-native: rows in ``mcp_token`` with ``token_type="direct"``,
verified by the existing ``MCPTokenVerifier``. They let a client connect to a
single connector without an account, acting under the connector owner's
identity. Generation is owner-only and gated by ``connector.allow_token_access``
(enforced at the route layer).

One-time reveal is enforced at the projection layer: the full token value lives
in ``mcp_token.token`` (the verifier looks it up by exact match) but is only ever
returned on creation. List/get responses expose only the 8-char prefix.
"""
import secrets
import uuid
from datetime import datetime, timedelta, UTC

from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.models.mcp.mcp_connector import MCPConnector
from app.models.mcp.mcp_token import (
    MCPToken,
    MCPConnectorTokenCreated,
    MCPConnectorTokenPublic,
)

# Direct tokens are very long-lived (mirrors A2A access tokens). The expiry
# check stays meaningful while behaving as "effectively never expires".
DIRECT_TOKEN_TTL = timedelta(days=365 * 5)
DIRECT_TOKEN_SCOPE = "mcp:tools mcp:resources"
DIRECT_TOKEN_CLIENT_ID = "direct"  # sentinel — direct tokens have no OAuth client


class MCPDirectTokenService:
    @staticmethod
    def to_public(token: MCPToken) -> MCPConnectorTokenPublic:
        return MCPConnectorTokenPublic(
            id=token.id,
            connector_id=token.connector_id,
            label=token.label,
            prefix=token.token[:8],
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            revoked=token.revoked,
            expires_at=token.expires_at,
        )

    @staticmethod
    def create_token(
        db_session: DBSession,
        connector: MCPConnector,
        label: str,
    ) -> MCPConnectorTokenCreated:
        """Mint a connector-scoped direct token.

        Ownership and ``allow_token_access`` are verified by the caller (route).
        Returns the full token value — this is the only time it is exposed.
        """
        token_value = secrets.token_urlsafe(48)
        resource = (
            f"{settings.MCP_SERVER_BASE_URL}/{connector.id}/mcp"
            if settings.MCP_SERVER_BASE_URL
            else ""
        )
        token = MCPToken(
            token=token_value,
            token_type="direct",
            client_id=DIRECT_TOKEN_CLIENT_ID,
            user_id=connector.owner_id,
            connector_id=connector.id,
            scope=DIRECT_TOKEN_SCOPE,
            resource=resource,
            label=label,
            expires_at=datetime.now(UTC) + DIRECT_TOKEN_TTL,
        )
        db_session.add(token)
        db_session.commit()
        db_session.refresh(token)

        public = MCPDirectTokenService.to_public(token)
        return MCPConnectorTokenCreated(**public.model_dump(), token=token_value)

    @staticmethod
    def list_tokens(
        db_session: DBSession,
        connector_id: uuid.UUID,
    ) -> list[MCPConnectorTokenPublic]:
        statement = (
            select(MCPToken)
            .where(
                MCPToken.connector_id == connector_id,
                MCPToken.token_type == "direct",
            )
            .order_by(MCPToken.created_at.desc())
        )
        tokens = db_session.exec(statement).all()
        return [MCPDirectTokenService.to_public(t) for t in tokens]

    @staticmethod
    def get_token(
        db_session: DBSession,
        token_id: uuid.UUID,
        connector_id: uuid.UUID,
    ) -> MCPToken | None:
        """Load a direct token, verifying it belongs to the connector."""
        token = db_session.get(MCPToken, token_id)
        if (
            not token
            or token.connector_id != connector_id
            or token.token_type != "direct"
        ):
            return None
        return token

    @staticmethod
    def set_revoked(
        db_session: DBSession,
        token: MCPToken,
        revoked: bool,
    ) -> MCPConnectorTokenPublic:
        token.revoked = revoked
        db_session.add(token)
        db_session.commit()
        db_session.refresh(token)
        return MCPDirectTokenService.to_public(token)

    @staticmethod
    def delete_token(
        db_session: DBSession,
        token: MCPToken,
    ) -> None:
        db_session.delete(token)
        db_session.commit()
