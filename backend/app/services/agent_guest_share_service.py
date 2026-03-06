"""
Agent Guest Share Service - Business logic for guest share link operations.
"""
import uuid
import random
import secrets
import hashlib
import logging
from datetime import UTC, datetime, timedelta

import jwt
from sqlmodel import Session, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import (
    Agent,
    AgentGuestShare,
    AgentGuestShareCreate,
    AgentGuestShareUpdate,
    AgentGuestSharePublic,
    AgentGuestShareCreated,
    AgentGuestSharesPublic,
    GuestShareGrant,
)
from app.models.session import Session as AgentSession
from app.core.config import settings
from app.core.security import ALGORITHM, encrypt_field, decrypt_field

logger = logging.getLogger(__name__)


class AgentGuestShareService:
    """
    Service for managing agent guest share links.

    Responsibilities:
    - Generate share tokens for guest access
    - Store token hashes (not actual tokens)
    - Validate guest share tokens
    - CRUD operations for guest shares
    """

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        """
        Create SHA256 hash of a token for secure storage.

        Args:
            raw_token: The raw token string

        Returns:
            Hex-encoded SHA256 hash
        """
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @staticmethod
    def _verify_security_code(
        session: Session,
        guest_share: AgentGuestShare,
        provided_code: str | None,
    ) -> None:
        """
        Verify the security code for a guest share.

        Args:
            session: Database session
            guest_share: The guest share record
            provided_code: The code provided by the guest

        Raises:
            ValueError: If the code is incorrect, missing, or the share is blocked
        """
        # No code set (backward compat with old shares)
        if guest_share.security_code_encrypted is None:
            return

        # Already blocked
        if guest_share.is_code_blocked:
            raise ValueError("This share link has been blocked due to too many failed attempts. Contact the owner.")

        # Code required but not provided
        if not provided_code:
            raise ValueError("Security code is required")

        # Decrypt and compare
        stored_code = decrypt_field(guest_share.security_code_encrypted)
        if provided_code != stored_code:
            guest_share.failed_code_attempts += 1
            if guest_share.failed_code_attempts >= 3:
                guest_share.is_code_blocked = True
                session.add(guest_share)
                session.commit()
                raise ValueError("This share link has been blocked due to too many failed attempts. Contact the owner.")
            remaining = 3 - guest_share.failed_code_attempts
            session.add(guest_share)
            session.commit()
            raise ValueError(f"Incorrect security code. {remaining} attempt(s) remaining.")

    @staticmethod
    def create_guest_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        data: AgentGuestShareCreate,
    ) -> AgentGuestShareCreated:
        """
        Create a new guest share link for an agent.

        Args:
            session: Database session
            user_id: User ID (owner)
            agent_id: Agent ID
            data: Guest share creation data

        Returns:
            Created guest share info including the actual token and share URL
            (shown only once)

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        # Generate token
        token = secrets.token_urlsafe(32)
        token_hash = AgentGuestShareService._hash_token(token)
        token_prefix = token[:8]

        # Generate security code
        security_code = f"{random.randint(0, 9999):04d}"
        security_code_encrypted = encrypt_field(security_code)

        # Calculate expiration
        expires_at = datetime.now(UTC) + timedelta(hours=data.expires_in_hours)

        # Create DB record
        guest_share = AgentGuestShare(
            agent_id=agent_id,
            owner_id=user_id,
            label=data.label,
            token_hash=token_hash,
            token_prefix=token_prefix,
            token=token,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
            security_code_encrypted=security_code_encrypted,
            allow_env_panel=data.allow_env_panel,
        )
        session.add(guest_share)
        session.commit()
        session.refresh(guest_share)

        # Construct share URL
        share_url = f"{settings.FRONTEND_HOST}/guest/{token}"

        logger.info(
            f"Created guest share {guest_share.id} for agent {agent_id}"
        )

        return AgentGuestShareCreated(
            id=guest_share.id,
            agent_id=guest_share.agent_id,
            label=guest_share.label,
            token_prefix=guest_share.token_prefix,
            expires_at=guest_share.expires_at,
            created_at=guest_share.created_at,
            is_revoked=guest_share.is_revoked,
            session_count=0,
            token=token,
            share_url=share_url,
            security_code=security_code,
            allow_env_panel=guest_share.allow_env_panel,
        )

    @staticmethod
    def list_guest_shares(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> AgentGuestSharesPublic:
        """
        List all guest shares for an agent.

        Args:
            session: Database session
            user_id: User ID (for ownership verification)
            agent_id: Agent ID

        Returns:
            List of guest shares with session counts

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        shares = session.exec(
            select(AgentGuestShare)
            .where(AgentGuestShare.agent_id == agent_id)
            .order_by(AgentGuestShare.created_at.desc())
        ).all()

        # Compute session counts for each share
        result = []
        for share in shares:
            session_count = session.exec(
                select(func.count()).where(
                    AgentSession.guest_share_id == share.id
                )
            ).one()

            share_url = f"{settings.FRONTEND_HOST}/guest/{share.token}" if share.token else None
            security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None
            result.append(
                AgentGuestSharePublic(
                    id=share.id,
                    agent_id=share.agent_id,
                    label=share.label,
                    token_prefix=share.token_prefix,
                    expires_at=share.expires_at,
                    created_at=share.created_at,
                    is_revoked=share.is_revoked,
                    session_count=session_count,
                    share_url=share_url,
                    security_code=security_code,
                    is_code_blocked=share.is_code_blocked,
                    allow_env_panel=share.allow_env_panel,
                )
            )

        return AgentGuestSharesPublic(data=result, count=len(result))

    @staticmethod
    def get_guest_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        guest_share_id: uuid.UUID,
    ) -> AgentGuestSharePublic | None:
        """
        Get a specific guest share by ID.

        Args:
            session: Database session
            user_id: User ID (for ownership verification)
            agent_id: Agent ID
            guest_share_id: Guest share ID

        Returns:
            Guest share with session count, or None if not found

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        share = session.exec(
            select(AgentGuestShare).where(
                AgentGuestShare.id == guest_share_id,
                AgentGuestShare.agent_id == agent_id,
            )
        ).first()

        if not share:
            return None

        # Compute session count
        session_count = session.exec(
            select(func.count()).where(
                AgentSession.guest_share_id == share.id
            )
        ).one()

        share_url = f"{settings.FRONTEND_HOST}/guest/{share.token}" if share.token else None
        security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None
        return AgentGuestSharePublic(
            id=share.id,
            agent_id=share.agent_id,
            label=share.label,
            token_prefix=share.token_prefix,
            expires_at=share.expires_at,
            created_at=share.created_at,
            is_revoked=share.is_revoked,
            session_count=session_count,
            share_url=share_url,
            security_code=security_code,
            is_code_blocked=share.is_code_blocked,
            allow_env_panel=share.allow_env_panel,
        )

    @staticmethod
    def delete_guest_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        guest_share_id: uuid.UUID,
    ) -> bool:
        """
        Delete a guest share link.

        Args:
            session: Database session
            user_id: User ID (for ownership verification)
            agent_id: Agent ID
            guest_share_id: Guest share ID

        Returns:
            True if deleted, False if not found

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        share = session.exec(
            select(AgentGuestShare).where(
                AgentGuestShare.id == guest_share_id,
                AgentGuestShare.agent_id == agent_id,
            )
        ).first()

        if not share:
            return False

        session.delete(share)
        session.commit()

        logger.info(f"Deleted guest share {guest_share_id}")
        return True

    @staticmethod
    def update_guest_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        guest_share_id: uuid.UUID,
        data: "AgentGuestShareUpdate",
    ) -> AgentGuestSharePublic | None:
        """
        Update a guest share (label and/or security code).

        If a new security code is provided, it resets failed_code_attempts
        and is_code_blocked, allowing a previously blocked link to be unblocked.

        Args:
            session: Database session
            user_id: User ID (for ownership verification)
            agent_id: Agent ID
            guest_share_id: Guest share ID
            data: Update data

        Returns:
            Updated guest share public data, or None if not found

        Raises:
            ValueError: If agent not found or not owned by user
        """
        # Verify agent ownership
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Agent not owned by user")

        share = session.exec(
            select(AgentGuestShare).where(
                AgentGuestShare.id == guest_share_id,
                AgentGuestShare.agent_id == agent_id,
            )
        ).first()

        if not share:
            return None

        if data.label is not None:
            share.label = data.label

        if data.security_code is not None:
            share.security_code_encrypted = encrypt_field(data.security_code)
            share.failed_code_attempts = 0
            share.is_code_blocked = False

        if data.allow_env_panel is not None:
            share.allow_env_panel = data.allow_env_panel

        session.add(share)
        session.commit()
        session.refresh(share)

        # Compute session count
        session_count = session.exec(
            select(func.count()).where(
                AgentSession.guest_share_id == share.id
            )
        ).one()

        share_url = f"{settings.FRONTEND_HOST}/guest/{share.token}" if share.token else None
        security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None

        logger.info(f"Updated guest share {guest_share_id}")

        return AgentGuestSharePublic(
            id=share.id,
            agent_id=share.agent_id,
            label=share.label,
            token_prefix=share.token_prefix,
            expires_at=share.expires_at,
            created_at=share.created_at,
            is_revoked=share.is_revoked,
            session_count=session_count,
            share_url=share_url,
            security_code=security_code,
            is_code_blocked=share.is_code_blocked,
            allow_env_panel=share.allow_env_panel,
        )

    @staticmethod
    def validate_token(
        session: Session,
        raw_token: str,
    ) -> AgentGuestShare | None:
        """
        Validate a raw guest share token.

        Args:
            session: Database session
            raw_token: The raw token string from the guest URL

        Returns:
            The guest share record if valid, None otherwise
        """
        token_hash = AgentGuestShareService._hash_token(raw_token)

        share = session.exec(
            select(AgentGuestShare).where(
                AgentGuestShare.token_hash == token_hash,
                AgentGuestShare.is_revoked == False,
                AgentGuestShare.expires_at > datetime.now(UTC),
            )
        ).first()

        return share

    @staticmethod
    def _find_share_by_token(
        session: Session,
        raw_token: str,
    ) -> AgentGuestShare | None:
        """
        Find a guest share by raw token without validity checks.

        Used to distinguish between "not found" (token doesn't exist)
        and "expired/revoked" (token exists but is no longer valid).

        Args:
            session: Database session
            raw_token: The raw token string

        Returns:
            The guest share record regardless of validity, or None if not found
        """
        token_hash = AgentGuestShareService._hash_token(raw_token)
        return session.exec(
            select(AgentGuestShare).where(
                AgentGuestShare.token_hash == token_hash,
            )
        ).first()

    @staticmethod
    def _create_guest_jwt(guest_share: AgentGuestShare) -> str:
        """
        Create a guest JWT token for anonymous access.

        The JWT lifetime is capped at 24 hours but never exceeds
        the guest share link expiry time.

        Args:
            guest_share: The validated guest share record

        Returns:
            Encoded JWT token string
        """
        now = datetime.now(UTC)
        max_expiry = now + timedelta(hours=24)
        # Cap at 24h but never exceed the share link's own expiry
        # Ensure both are timezone-aware for comparison
        share_expires = guest_share.expires_at
        if share_expires.tzinfo is None:
            share_expires = share_expires.replace(tzinfo=UTC)
        exp = min(share_expires, max_expiry)

        payload = {
            "sub": str(guest_share.id),
            "role": "chat-guest",
            "agent_id": str(guest_share.agent_id),
            "owner_id": str(guest_share.owner_id),
            "token_type": "guest_share",
            "exp": exp,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def authenticate_anonymous(
        session: Session,
        raw_token: str,
        security_code: str | None = None,
    ) -> dict | None:
        """
        Authenticate an anonymous visitor via a guest share token.

        Validates the token and issues a short-lived guest JWT for
        anonymous chat access.

        Args:
            session: Database session
            raw_token: The raw token from the guest share URL
            security_code: Optional 4-digit security code

        Returns:
            Dict with access_token, token_type, guest_share_id, agent_id
            if valid. None if the token does not exist at all.

        Raises:
            ValueError: If the token exists but is expired or revoked,
                or if the security code is invalid
        """
        guest_share = AgentGuestShareService.validate_token(session, raw_token)

        if not guest_share:
            # Check if the token exists but is expired/revoked
            existing = AgentGuestShareService._find_share_by_token(session, raw_token)
            if existing:
                raise ValueError("Guest share link has expired or been revoked")
            return None

        # Verify security code
        AgentGuestShareService._verify_security_code(session, guest_share, security_code)

        jwt_token = AgentGuestShareService._create_guest_jwt(guest_share)

        logger.info(
            f"Anonymous auth via guest share {guest_share.id} for agent {guest_share.agent_id}"
        )

        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "guest_share_id": str(guest_share.id),
            "agent_id": str(guest_share.agent_id),
        }

    @staticmethod
    def activate_for_user(
        session: Session,
        raw_token: str,
        user_id: uuid.UUID,
        security_code: str | None = None,
    ) -> dict | None:
        """
        Activate a guest share grant for an authenticated user.

        Creates a GuestShareGrant record (idempotent — duplicate
        activations are silently ignored via ON CONFLICT DO NOTHING).

        Args:
            session: Database session
            raw_token: The raw token from the guest share URL
            user_id: The authenticated user's ID
            security_code: Optional 4-digit security code

        Returns:
            Dict with guest_share_id, agent_id, agent_name if valid.
            None if the token does not exist at all.

        Raises:
            ValueError: If the token exists but is expired or revoked,
                or if the security code is invalid
        """
        guest_share = AgentGuestShareService.validate_token(session, raw_token)

        if not guest_share:
            existing = AgentGuestShareService._find_share_by_token(session, raw_token)
            if existing:
                raise ValueError("Guest share link has expired or been revoked")
            return None

        # Verify security code
        AgentGuestShareService._verify_security_code(session, guest_share, security_code)

        # UPSERT grant: INSERT ... ON CONFLICT (user_id, guest_share_id) DO NOTHING
        stmt = pg_insert(GuestShareGrant).values(
            id=uuid.uuid4(),
            user_id=user_id,
            guest_share_id=guest_share.id,
            activated_at=datetime.now(UTC),
        ).on_conflict_do_nothing(
            constraint="uq_guest_share_grant_user_share"
        )
        session.exec(stmt)
        session.commit()

        # Fetch agent name
        agent = session.get(Agent, guest_share.agent_id)
        agent_name = agent.name if agent else "Unknown Agent"

        logger.info(
            f"User {user_id} activated guest share {guest_share.id} for agent {guest_share.agent_id}"
        )

        return {
            "guest_share_id": str(guest_share.id),
            "agent_id": str(guest_share.agent_id),
            "agent_name": agent_name,
        }

    @staticmethod
    def get_guest_share_info(
        session: Session,
        raw_token: str,
    ) -> dict:
        """
        Get public information about a guest share link.

        This endpoint is used by the frontend to display agent info
        on the guest landing page before authentication.

        Args:
            session: Database session
            raw_token: The raw token from the guest share URL

        Returns:
            Dict with agent_name, agent_description, is_valid, guest_share_id.
            If the token is invalid, is_valid is False.
        """
        guest_share = AgentGuestShareService.validate_token(session, raw_token)

        if not guest_share:
            # Check if it existed at all
            existing = AgentGuestShareService._find_share_by_token(session, raw_token)
            if existing:
                return {
                    "agent_name": None,
                    "agent_description": None,
                    "is_valid": False,
                    "guest_share_id": str(existing.id),
                    "requires_code": False,
                    "is_code_blocked": False,
                    "allow_env_panel": False,
                }
            return {
                "agent_name": None,
                "agent_description": None,
                "is_valid": False,
                "guest_share_id": None,
                "requires_code": False,
                "is_code_blocked": False,
                "allow_env_panel": False,
            }

        # Fetch agent info
        agent = session.get(Agent, guest_share.agent_id)
        agent_name = agent.name if agent else "Unknown Agent"
        agent_description = None
        if agent and agent.description:
            agent_description = agent.description[:200]

        return {
            "agent_name": agent_name,
            "agent_description": agent_description,
            "is_valid": True,
            "guest_share_id": str(guest_share.id),
            "requires_code": guest_share.security_code_encrypted is not None,
            "is_code_blocked": guest_share.is_code_blocked,
            "allow_env_panel": guest_share.allow_env_panel,
        }

    @staticmethod
    def check_grant(
        session: Session,
        user_id: uuid.UUID,
        guest_share_id: uuid.UUID,
    ) -> bool:
        """
        Check if a user has an active grant for a guest share.

        Verifies both the grant record exists AND the parent guest share
        is still valid (not expired, not revoked).

        Args:
            session: Database session
            user_id: The user's ID
            guest_share_id: The guest share ID

        Returns:
            True if the user has a valid grant, False otherwise
        """
        # Check the grant exists
        grant = session.exec(
            select(GuestShareGrant).where(
                GuestShareGrant.user_id == user_id,
                GuestShareGrant.guest_share_id == guest_share_id,
            )
        ).first()

        if not grant:
            return False

        # Verify the parent guest share is still valid
        guest_share = session.get(AgentGuestShare, guest_share_id)
        if not guest_share:
            return False
        if guest_share.is_revoked:
            return False
        # Ensure timezone-aware comparison
        expires_at = guest_share.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return False

        return True
