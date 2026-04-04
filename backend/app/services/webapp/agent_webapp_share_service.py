"""
Agent Webapp Share Service - Business logic for webapp share link operations.
"""
import uuid
import random
import secrets
import hashlib
import logging
from datetime import UTC, datetime, timedelta

import jwt
from sqlmodel import Session, select

from app.models import (
    Agent,
    AgentWebappShare,
    AgentWebappShareCreate,
    AgentWebappShareUpdate,
    AgentWebappSharePublic,
    AgentWebappShareCreated,
    AgentWebappSharesPublic,
)
from app.core.config import settings
from app.core.security import ALGORITHM, encrypt_field, decrypt_field
from app.services.webapp.agent_webapp_interface_config_service import AgentWebappInterfaceConfigService

logger = logging.getLogger(__name__)


class WebappShareError(Exception):
    """Base exception for webapp share service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ShareAgentNotFoundError(WebappShareError):
    def __init__(self):
        super().__init__("Agent not found", status_code=404)


class ShareAgentNotOwnedError(WebappShareError):
    # Returns 404 to avoid leaking agent existence info
    def __init__(self):
        super().__init__("Agent not found", status_code=404)


class ShareWebappDisabledError(WebappShareError):
    def __init__(self):
        super().__init__("Webapp feature is disabled for this agent", status_code=400)


class ShareNotFoundError(WebappShareError):
    def __init__(self):
        super().__init__("Webapp share not found", status_code=404)


class ShareExpiredError(WebappShareError):
    def __init__(self):
        super().__init__("Webapp share link has expired or been deactivated", status_code=410)


class SecurityCodeError(WebappShareError):
    def __init__(self, message: str):
        super().__init__(message, status_code=403)


class AgentWebappShareService:
    """Service for managing agent webapp share links."""

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @staticmethod
    def _verify_agent_ownership(
        session: Session, user_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Agent:
        """Verify agent exists and is owned by user. Returns the agent."""
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ShareAgentNotFoundError()
        if agent.owner_id != user_id:
            raise ShareAgentNotOwnedError()
        return agent

    @staticmethod
    def _verify_security_code(
        session: Session,
        share: AgentWebappShare,
        provided_code: str | None,
    ) -> None:
        if share.security_code_encrypted is None:
            return
        if share.is_code_blocked:
            raise SecurityCodeError("This share link has been blocked due to too many failed attempts.")
        if not provided_code:
            raise SecurityCodeError("Security code is required")

        stored_code = decrypt_field(share.security_code_encrypted)
        if provided_code != stored_code:
            share.failed_code_attempts += 1
            if share.failed_code_attempts >= 3:
                share.is_code_blocked = True
                session.add(share)
                session.commit()
                raise SecurityCodeError("This share link has been blocked due to too many failed attempts.")
            remaining = 3 - share.failed_code_attempts
            session.add(share)
            session.commit()
            raise SecurityCodeError(f"Incorrect security code. {remaining} attempt(s) remaining.")

    @staticmethod
    def create_webapp_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        data: AgentWebappShareCreate,
    ) -> AgentWebappShareCreated:
        agent = AgentWebappShareService._verify_agent_ownership(
            session, user_id, agent_id
        )
        if not agent.webapp_enabled:
            raise ShareWebappDisabledError()

        token = secrets.token_urlsafe(32)
        token_hash = AgentWebappShareService._hash_token(token)
        token_prefix = token[:8]

        security_code = None
        security_code_encrypted = None
        if data.require_security_code:
            security_code = f"{random.randint(0, 9999):04d}"
            security_code_encrypted = encrypt_field(security_code)

        expires_at = None
        if data.expires_in_hours:
            expires_at = datetime.now(UTC) + timedelta(hours=data.expires_in_hours)

        share = AgentWebappShare(
            agent_id=agent_id,
            owner_id=user_id,
            label=data.label,
            token_hash=token_hash,
            token_prefix=token_prefix,
            token=token,
            allow_data_api=data.allow_data_api,
            security_code_encrypted=security_code_encrypted,
            expires_at=expires_at,
        )
        session.add(share)
        session.commit()
        session.refresh(share)

        share_url = f"{settings.FRONTEND_HOST}/webapp/{token}"

        logger.info(f"Created webapp share {share.id} for agent {agent_id}")

        return AgentWebappShareCreated(
            id=share.id,
            agent_id=share.agent_id,
            label=share.label,
            token_prefix=share.token_prefix,
            is_active=share.is_active,
            allow_data_api=share.allow_data_api,
            expires_at=share.expires_at,
            created_at=share.created_at,
            token=token,
            share_url=share_url,
            security_code=security_code,
        )

    @staticmethod
    def list_webapp_shares(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> AgentWebappSharesPublic:
        AgentWebappShareService._verify_agent_ownership(
            session, user_id, agent_id
        )

        shares = session.exec(
            select(AgentWebappShare)
            .where(AgentWebappShare.agent_id == agent_id)
            .order_by(AgentWebappShare.created_at.desc())
        ).all()

        result = []
        for share in shares:
            share_url = f"{settings.FRONTEND_HOST}/webapp/{share.token}" if share.token else None
            security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None
            result.append(
                AgentWebappSharePublic(
                    id=share.id,
                    agent_id=share.agent_id,
                    label=share.label,
                    token_prefix=share.token_prefix,
                    is_active=share.is_active,
                    allow_data_api=share.allow_data_api,
                    expires_at=share.expires_at,
                    created_at=share.created_at,
                    share_url=share_url,
                    security_code=security_code,
                    is_code_blocked=share.is_code_blocked,
                )
            )

        return AgentWebappSharesPublic(data=result, count=len(result))

    @staticmethod
    def update_webapp_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        share_id: uuid.UUID,
        data: AgentWebappShareUpdate,
    ) -> AgentWebappSharePublic | None:
        AgentWebappShareService._verify_agent_ownership(
            session, user_id, agent_id
        )

        share = session.exec(
            select(AgentWebappShare).where(
                AgentWebappShare.id == share_id,
                AgentWebappShare.agent_id == agent_id,
            )
        ).first()

        if not share:
            return None

        if data.label is not None:
            share.label = data.label
        if data.is_active is not None:
            share.is_active = data.is_active
        if data.allow_data_api is not None:
            share.allow_data_api = data.allow_data_api
        if data.remove_security_code:
            share.security_code_encrypted = None
            share.failed_code_attempts = 0
            share.is_code_blocked = False
        elif data.security_code is not None:
            share.security_code_encrypted = encrypt_field(data.security_code)
            share.failed_code_attempts = 0
            share.is_code_blocked = False

        share.updated_at = datetime.now(UTC)
        session.add(share)
        session.commit()
        session.refresh(share)

        share_url = f"{settings.FRONTEND_HOST}/webapp/{share.token}" if share.token else None
        security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None

        return AgentWebappSharePublic(
            id=share.id,
            agent_id=share.agent_id,
            label=share.label,
            token_prefix=share.token_prefix,
            is_active=share.is_active,
            allow_data_api=share.allow_data_api,
            expires_at=share.expires_at,
            created_at=share.created_at,
            share_url=share_url,
            security_code=security_code,
            is_code_blocked=share.is_code_blocked,
        )

    @staticmethod
    def delete_webapp_share(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        share_id: uuid.UUID,
    ) -> bool:
        AgentWebappShareService._verify_agent_ownership(
            session, user_id, agent_id
        )

        share = session.exec(
            select(AgentWebappShare).where(
                AgentWebappShare.id == share_id,
                AgentWebappShare.agent_id == agent_id,
            )
        ).first()

        if not share:
            return False

        session.delete(share)
        session.commit()
        logger.info(f"Deleted webapp share {share_id}")
        return True

    @staticmethod
    def get_first_active_share_info(
        session: Session, agent_id: uuid.UUID,
    ) -> tuple[str, str | None] | None:
        """Return (share_url, security_code_or_None) for the first active, non-expired share, or None."""
        now = datetime.now(UTC)
        shares = session.exec(
            select(AgentWebappShare)
            .where(
                AgentWebappShare.agent_id == agent_id,
                AgentWebappShare.is_active == True,
            )
            .order_by(AgentWebappShare.created_at.asc())
        ).all()

        for share in shares:
            if share.expires_at:
                expires_at = share.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    continue
            share_url = f"{settings.FRONTEND_HOST}/webapp/{share.token}"
            security_code = decrypt_field(share.security_code_encrypted) if share.security_code_encrypted else None
            return share_url, security_code
        return None

    @staticmethod
    def validate_token(session: Session, raw_token: str) -> AgentWebappShare | None:
        token_hash = AgentWebappShareService._hash_token(raw_token)
        now = datetime.now(UTC)

        share = session.exec(
            select(AgentWebappShare).where(
                AgentWebappShare.token_hash == token_hash,
                AgentWebappShare.is_active == True,
            )
        ).first()

        if not share:
            return None

        # Check expiration
        if share.expires_at:
            expires_at = share.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                return None

        return share

    @staticmethod
    def _find_share_by_token(session: Session, raw_token: str) -> AgentWebappShare | None:
        token_hash = AgentWebappShareService._hash_token(raw_token)
        return session.exec(
            select(AgentWebappShare).where(AgentWebappShare.token_hash == token_hash)
        ).first()

    @staticmethod
    def _create_webapp_jwt(share: AgentWebappShare) -> str:
        now = datetime.now(UTC)
        max_expiry = now + timedelta(hours=24)

        if share.expires_at:
            share_expires = share.expires_at
            if share_expires.tzinfo is None:
                share_expires = share_expires.replace(tzinfo=UTC)
            exp = min(share_expires, max_expiry)
        else:
            exp = max_expiry

        payload = {
            "sub": str(share.id),
            "role": "webapp-viewer",
            "agent_id": str(share.agent_id),
            "owner_id": str(share.owner_id),
            "token_type": "webapp_share",
            "allow_data_api": share.allow_data_api,
            "exp": exp,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def authenticate(
        session: Session,
        raw_token: str,
        security_code: str | None = None,
    ) -> dict | None:
        share = AgentWebappShareService.validate_token(session, raw_token)

        if not share:
            existing = AgentWebappShareService._find_share_by_token(session, raw_token)
            if existing:
                raise ShareExpiredError()
            raise ShareNotFoundError()

        AgentWebappShareService._verify_security_code(session, share, security_code)

        jwt_token = AgentWebappShareService._create_webapp_jwt(share)

        logger.info(f"Webapp share auth for share {share.id}, agent {share.agent_id}")

        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "webapp_share_id": str(share.id),
            "agent_id": str(share.agent_id),
        }

    @staticmethod
    def get_share_info(session: Session, raw_token: str) -> dict:
        """Get public information about a share. Does not raise — returns is_valid=False for invalid tokens."""
        share = AgentWebappShareService.validate_token(session, raw_token)

        if not share:
            existing = AgentWebappShareService._find_share_by_token(session, raw_token)
            if existing:
                return {
                    "agent_name": None,
                    "is_valid": False,
                    "webapp_share_id": str(existing.id),
                    "requires_code": False,
                    "is_code_blocked": False,
                }
            return {
                "agent_name": None,
                "is_valid": False,
                "webapp_share_id": None,
                "requires_code": False,
                "is_code_blocked": False,
            }

        agent = session.get(Agent, share.agent_id)
        agent_name = agent.name if agent else "Unknown Agent"

        interface_config = AgentWebappInterfaceConfigService.get_by_agent_id(session, share.agent_id)

        return {
            "agent_name": agent_name,
            "is_valid": True,
            "webapp_share_id": str(share.id),
            "requires_code": share.security_code_encrypted is not None,
            "is_code_blocked": share.is_code_blocked,
            "interface_config": interface_config,
        }
