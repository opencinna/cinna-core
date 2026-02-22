"""
Agent Workspace Token Service - short-lived JWTs for public agent workspace file access.

Used by A2A clients to open agent workspace file links in a browser without
requiring regular user authentication.
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import settings
from app.core.security import ALGORITHM

logger = logging.getLogger(__name__)

# Token validity duration
WORKSPACE_VIEW_TOKEN_EXPIRY = timedelta(hours=1)


class AgentWorkspaceTokenService:
    """Service for creating and verifying agent workspace view tokens."""

    @staticmethod
    def create_workspace_view_token(env_id: UUID, agent_id: UUID) -> str:
        """
        Create a short-lived JWT for workspace file viewing.

        Args:
            env_id: Environment UUID
            agent_id: Agent UUID

        Returns:
            Signed JWT string
        """
        expire = datetime.now(timezone.utc) + WORKSPACE_VIEW_TOKEN_EXPIRY
        payload = {
            "type": "workspace_view",
            "env_id": str(env_id),
            "agent_id": str(agent_id),
            "exp": expire,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def verify_workspace_view_token(token: str) -> dict | None:
        """
        Decode and validate a workspace view token.

        Args:
            token: JWT string

        Returns:
            Token payload dict if valid, None otherwise
        """
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            if payload.get("type") != "workspace_view":
                logger.warning("Token type mismatch: expected 'workspace_view'")
                return None
            return payload
        except jwt.ExpiredSignatureError:
            logger.debug("Workspace view token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid workspace view token: {e}")
            return None
