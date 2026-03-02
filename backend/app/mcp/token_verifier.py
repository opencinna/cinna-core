import logging
from datetime import datetime, UTC

from mcp.server.auth.provider import TokenVerifier, AccessToken
from sqlmodel import Session as DBSession, select

from app.core.db import engine
from app.models.mcp_connector import MCPConnector
from app.models.mcp_token import MCPToken
from app.core.config import settings
from app.mcp.context_vars import mcp_authenticated_user_id_var

logger = logging.getLogger(__name__)


class MCPTokenVerifier(TokenVerifier):
    """Verifies MCP bearer tokens against the database."""

    def __init__(self, connector_id: str):
        self.connector_id = connector_id

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return an AccessToken if valid."""
        with DBSession(engine) as db:
            # Look up token
            token_record = db.exec(
                select(MCPToken).where(
                    MCPToken.token == token,
                    MCPToken.token_type == "access",
                )
            ).first()

            if not token_record:
                logger.debug("Token not found in database")
                return None

            # Check expiry (DB stores naive UTC datetimes)
            if token_record.expires_at < datetime.now(UTC).replace(tzinfo=None):
                logger.debug("Token expired")
                return None

            # Check revocation
            if token_record.revoked:
                logger.debug("Token revoked")
                return None

            # Verify connector match
            if str(token_record.connector_id) != self.connector_id:
                logger.debug("Token connector mismatch: %s != %s", token_record.connector_id, self.connector_id)
                return None

            # Verify connector is still active
            connector = db.get(MCPConnector, token_record.connector_id)
            if not connector or not connector.is_active:
                logger.debug("Connector not found or inactive")
                return None

            scopes = [s for s in token_record.scope.split(" ") if s] if token_record.scope else []
            expires_at_ts = int(token_record.expires_at.timestamp()) if token_record.expires_at else None

            # Propagate authenticated user identity to tool handlers via ContextVar.
            # Token not captured — cleanup is handled by MCPServerRegistry.__call__()
            # which resets mcp_authenticated_user_id_var in its finally block.
            mcp_authenticated_user_id_var.set(str(token_record.user_id))

            return AccessToken(
                token=token,
                client_id=token_record.client_id,
                scopes=scopes,
                expires_at=expires_at_ts,
                resource=token_record.resource or None,
            )
