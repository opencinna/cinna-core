"""
Access Token Service - Business logic for A2A access token operations.
"""
import uuid
import secrets
import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone

import jwt
from sqlmodel import Session, select

from app.models import (
    Agent,
    AgentAccessToken,
    AgentAccessTokenCreate,
    AgentAccessTokenUpdate,
    AgentAccessTokenPublic,
    AgentAccessTokenCreated,
    AccessTokenMode,
    AccessTokenScope,
    A2ATokenPayload,
)
from app.core.config import settings
from app.core.security import ALGORITHM

logger = logging.getLogger(__name__)

# Default token expiration: 5 years
DEFAULT_TOKEN_EXPIRY_DAYS = 5 * 365  # 1825 days


class AccessTokenService:
    """
    Service for managing A2A access tokens.

    Responsibilities:
    - Generate JWT tokens for A2A authentication
    - Store token hashes (not actual tokens)
    - Validate A2A tokens
    - CRUD operations for access tokens
    """

    @staticmethod
    def _hash_token(token: str) -> str:
        """
        Create SHA256 hash of a token for secure storage.

        Args:
            token: The JWT token string

        Returns:
            Hex-encoded SHA256 hash
        """
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _create_a2a_jwt(
        token_id: uuid.UUID,
        agent_id: uuid.UUID,
        mode: AccessTokenMode,
        scope: AccessTokenScope,
        expires_at: datetime,
    ) -> str:
        """
        Create a JWT token for A2A authentication.

        Args:
            token_id: UUID of the access token record
            agent_id: UUID of the agent this token is for
            mode: Access mode (conversation or building)
            scope: Access scope (limited or general)
            expires_at: Expiration datetime

        Returns:
            JWT token string
        """
        payload = {
            "sub": str(token_id),
            "agent_id": str(agent_id),
            "mode": mode.value,
            "scope": scope.value,
            "token_type": "agent",
            "exp": int(expires_at.timestamp()),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def verify_a2a_token(token: str) -> A2ATokenPayload | None:
        """
        Verify and decode an A2A JWT token.

        Args:
            token: JWT token string

        Returns:
            Decoded token payload if valid, None otherwise
        """
        try:
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=[ALGORITHM]
            )
            # Check if it's an A2A token
            if payload.get("token_type") != "agent":
                return None

            return A2ATokenPayload(
                sub=payload["sub"],
                agent_id=payload["agent_id"],
                mode=payload["mode"],
                scope=payload["scope"],
                token_type=payload["token_type"],
                exp=payload["exp"],
            )
        except jwt.ExpiredSignatureError:
            logger.warning("A2A token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid A2A token: {e}")
            return None

    @staticmethod
    def validate_token_for_agent(
        session: Session,
        token: str,
        agent_id: uuid.UUID,
    ) -> tuple[AgentAccessToken | None, A2ATokenPayload | None]:
        """
        Validate an A2A token for accessing a specific agent.

        Args:
            session: Database session
            token: JWT token string
            agent_id: UUID of the agent being accessed

        Returns:
            Tuple of (access_token_record, payload) if valid, (None, None) otherwise
        """
        # Verify the JWT
        payload = AccessTokenService.verify_a2a_token(token)
        if not payload:
            return None, None

        # Check if token is for this agent
        if payload.agent_id != str(agent_id):
            logger.warning(
                f"Token agent_id mismatch: {payload.agent_id} != {agent_id}"
            )
            return None, None

        # Verify token exists in database and is not revoked
        token_hash = AccessTokenService._hash_token(token)
        access_token = session.exec(
            select(AgentAccessToken).where(
                AgentAccessToken.id == uuid.UUID(payload.sub),
                AgentAccessToken.agent_id == agent_id,
                AgentAccessToken.token_hash == token_hash,
                AgentAccessToken.is_revoked == False,
            )
        ).first()

        if not access_token:
            logger.warning(f"A2A token not found or revoked: {payload.sub}")
            return None, None

        # Update last_used_at
        access_token.last_used_at = datetime.now(UTC)
        session.add(access_token)
        session.commit()

        return access_token, payload

    @staticmethod
    def create_token(
        session: Session,
        user_id: uuid.UUID,
        data: AgentAccessTokenCreate,
    ) -> AgentAccessTokenCreated:
        """
        Create a new A2A access token.

        Args:
            session: Database session
            user_id: User ID (owner)
            data: Token creation data

        Returns:
            Created token info including the actual token (shown only once)

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, data.agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        # Calculate expiration (5 years from now)
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=DEFAULT_TOKEN_EXPIRY_DAYS
        )

        # Create the token record first to get the ID
        access_token = AgentAccessToken(
            agent_id=data.agent_id,
            owner_id=user_id,
            name=data.name,
            mode=data.mode,
            scope=data.scope,
            token_hash="",  # Placeholder, will update
            token_prefix="",  # Placeholder, will update
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        session.add(access_token)
        session.flush()  # Get the ID

        # Create the JWT token
        jwt_token = AccessTokenService._create_a2a_jwt(
            token_id=access_token.id,
            agent_id=data.agent_id,
            mode=data.mode,
            scope=data.scope,
            expires_at=expires_at,
        )

        # Update with hash and prefix
        access_token.token_hash = AccessTokenService._hash_token(jwt_token)
        access_token.token_prefix = jwt_token[:8]

        session.add(access_token)
        session.commit()
        session.refresh(access_token)

        logger.info(
            f"Created A2A access token {access_token.id} for agent {data.agent_id}"
        )

        # Return with the actual token (only shown once)
        return AgentAccessTokenCreated(
            id=access_token.id,
            agent_id=access_token.agent_id,
            name=access_token.name,
            mode=access_token.mode,
            scope=access_token.scope,
            token_prefix=access_token.token_prefix,
            expires_at=access_token.expires_at,
            created_at=access_token.created_at,
            last_used_at=access_token.last_used_at,
            is_revoked=access_token.is_revoked,
            token=jwt_token,  # Only returned on creation
        )

    @staticmethod
    def get_agent_tokens(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[AgentAccessTokenPublic]:
        """
        Get all access tokens for an agent.

        Args:
            session: Database session
            agent_id: Agent ID
            user_id: User ID (for ownership verification)

        Returns:
            List of access tokens

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        tokens = session.exec(
            select(AgentAccessToken)
            .where(AgentAccessToken.agent_id == agent_id)
            .order_by(AgentAccessToken.created_at.desc())
        ).all()

        return [
            AgentAccessTokenPublic(
                id=t.id,
                agent_id=t.agent_id,
                name=t.name,
                mode=t.mode,
                scope=t.scope,
                token_prefix=t.token_prefix,
                expires_at=t.expires_at,
                created_at=t.created_at,
                last_used_at=t.last_used_at,
                is_revoked=t.is_revoked,
            )
            for t in tokens
        ]

    @staticmethod
    def get_token_by_id(
        session: Session,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentAccessTokenPublic | None:
        """
        Get access token by ID (with ownership check).

        Args:
            session: Database session
            token_id: Token ID
            user_id: User ID (for ownership verification)

        Returns:
            Token if found and owned by user, None otherwise
        """
        token = session.exec(
            select(AgentAccessToken).where(
                AgentAccessToken.id == token_id,
                AgentAccessToken.owner_id == user_id,
            )
        ).first()

        if not token:
            return None

        return AgentAccessTokenPublic(
            id=token.id,
            agent_id=token.agent_id,
            name=token.name,
            mode=token.mode,
            scope=token.scope,
            token_prefix=token.token_prefix,
            expires_at=token.expires_at,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            is_revoked=token.is_revoked,
        )

    @staticmethod
    def update_token(
        session: Session,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentAccessTokenUpdate,
    ) -> AgentAccessTokenPublic | None:
        """
        Update access token (name and revoked status only).

        Args:
            session: Database session
            token_id: Token ID
            user_id: User ID (for ownership verification)
            data: Update data

        Returns:
            Updated token if found and owned by user, None otherwise
        """
        token = session.exec(
            select(AgentAccessToken).where(
                AgentAccessToken.id == token_id,
                AgentAccessToken.owner_id == user_id,
            )
        ).first()

        if not token:
            return None

        if data.name is not None:
            token.name = data.name
        if data.is_revoked is not None:
            token.is_revoked = data.is_revoked

        session.add(token)
        session.commit()
        session.refresh(token)

        logger.info(f"Updated A2A access token {token_id}")

        return AgentAccessTokenPublic(
            id=token.id,
            agent_id=token.agent_id,
            name=token.name,
            mode=token.mode,
            scope=token.scope,
            token_prefix=token.token_prefix,
            expires_at=token.expires_at,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            is_revoked=token.is_revoked,
        )

    @staticmethod
    def delete_token(
        session: Session,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        """
        Delete access token.

        Args:
            session: Database session
            token_id: Token ID
            user_id: User ID (for ownership verification)

        Returns:
            True if deleted, False if not found or not owned by user
        """
        token = session.exec(
            select(AgentAccessToken).where(
                AgentAccessToken.id == token_id,
                AgentAccessToken.owner_id == user_id,
            )
        ).first()

        if not token:
            return False

        session.delete(token)
        session.commit()

        logger.info(f"Deleted A2A access token {token_id}")
        return True

    @staticmethod
    def can_access_session(
        payload: A2ATokenPayload,
        session_access_token_id: uuid.UUID | None,
    ) -> bool:
        """
        Check if a token can access a specific session based on scope.

        Args:
            payload: The A2A token payload
            session_access_token_id: The access_token_id of the session (None if created via UI)

        Returns:
            True if the token can access the session, False otherwise
        """
        # General scope can access all sessions
        if payload.scope == AccessTokenScope.GENERAL.value:
            return True

        # Limited scope can only access sessions created by this token
        if payload.scope == AccessTokenScope.LIMITED.value:
            return session_access_token_id == uuid.UUID(payload.sub)

        return False

    @staticmethod
    def has_general_scope(payload: A2ATokenPayload) -> bool:
        """
        Check if a token has general scope (can access all sessions).

        Args:
            payload: The A2A token payload

        Returns:
            True if the token has general scope, False otherwise
        """
        return payload.scope == AccessTokenScope.GENERAL.value

    @staticmethod
    def can_use_mode(payload: A2ATokenPayload, requested_mode: str) -> bool:
        """
        Check if a token can use a specific mode.

        Args:
            payload: The A2A token payload
            requested_mode: The mode requested ("conversation" or "building")

        Returns:
            True if the token can use the mode, False otherwise
        """
        # Building mode can use both modes
        if payload.mode == AccessTokenMode.BUILDING.value:
            return True

        # Conversation mode can only use conversation
        if payload.mode == AccessTokenMode.CONVERSATION.value:
            return requested_mode == "conversation"

        return False
