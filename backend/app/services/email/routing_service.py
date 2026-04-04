"""
Email Routing Service - Maps sender email to the correct clone agent.

Handles access control, auto-user-creation, auto-sharing, and clone readiness.
"""
import fnmatch
import logging
import uuid

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.sharing.agent_share import AgentShare, ShareSource, ShareStatus
from app.models.email.agent_email_integration import AgentEmailIntegration, AgentSessionMode
from app.models.environments.environment import AgentEnvironment
from app.models.users.user import User
from app.services.email.integration_service import EmailIntegrationService
from app.services.users.user_service import UserService

logger = logging.getLogger(__name__)


class EmailAccessDenied(Exception):
    """Raised when an email sender is not allowed to interact with the agent."""
    pass


class EmailRoutingService:

    @staticmethod
    async def route_email(
        session: Session,
        agent_id: uuid.UUID,
        sender_email: str,
    ) -> tuple[uuid.UUID, bool, str]:
        """
        Route an incoming email to the correct target agent.

        Returns: (target_agent_id, is_ready, session_mode)
          - target_agent_id: clone agent (clone mode) or the parent agent itself (owner mode)
          - is_ready: whether the target agent's environment is running
          - session_mode: "clone" or "owner"
        Raises: EmailAccessDenied if sender is not allowed.
        """
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration or not integration.enabled:
            raise EmailAccessDenied("Email integration is not enabled for this agent")

        sender_email = sender_email.strip().lower()

        # Owner mode: route directly to the parent agent (no clone)
        if integration.agent_session_mode == AgentSessionMode.OWNER:
            # Access control still applies in owner mode
            if not EmailRoutingService._check_access_allowed(session, integration, sender_email):
                raise EmailAccessDenied(
                    f"Email from {sender_email} is not allowed for this agent"
                )
            is_ready = EmailRoutingService._is_clone_ready(session, agent_id)
            return agent_id, is_ready, AgentSessionMode.OWNER

        # Clone mode (default): each sender gets their own isolated clone
        # 1. Check for existing clone
        clone_id = EmailRoutingService._find_existing_clone(session, agent_id, sender_email)
        if clone_id:
            is_ready = EmailRoutingService._is_clone_ready(session, clone_id)
            return clone_id, is_ready, AgentSessionMode.CLONE

        # 2. Check access
        if not EmailRoutingService._check_access_allowed(session, integration, sender_email):
            raise EmailAccessDenied(
                f"Email from {sender_email} is not allowed for this agent"
            )

        # 3. Check max_clones limit
        current_count = EmailIntegrationService.get_email_clone_count(session, agent_id)
        if current_count >= integration.max_clones:
            raise EmailAccessDenied(
                f"Maximum email clone limit ({integration.max_clones}) reached"
            )

        # 4. Ensure user exists
        user_id = EmailRoutingService._ensure_user_exists(session, sender_email)

        # 5. Check for pending share that can be auto-accepted
        clone_id = await EmailRoutingService._auto_accept_pending_share(
            session, agent_id, user_id
        )
        if clone_id:
            is_ready = EmailRoutingService._is_clone_ready(session, clone_id)
            return clone_id, is_ready, AgentSessionMode.CLONE

        # 6. Create auto share + clone
        clone_id = await EmailRoutingService._auto_create_share_and_clone(
            session, agent_id, user_id, integration.clone_share_mode
        )
        is_ready = EmailRoutingService._is_clone_ready(session, clone_id)
        return clone_id, is_ready, AgentSessionMode.CLONE

    @staticmethod
    def _find_existing_clone(
        session: Session,
        agent_id: uuid.UUID,
        sender_email: str,
    ) -> uuid.UUID | None:
        """Find an existing clone for the sender via email integration shares."""
        # Find user by email
        user = UserService.get_user_by_email(session=session, email=sender_email)
        if not user:
            return None

        # Find accepted share with clone for this agent+user
        stmt = select(AgentShare).where(
            AgentShare.original_agent_id == agent_id,
            AgentShare.shared_with_user_id == user.id,
            AgentShare.status == ShareStatus.ACCEPTED,
            AgentShare.cloned_agent_id.isnot(None),  # type: ignore
        )
        share = session.exec(stmt).first()
        if share and share.cloned_agent_id:
            return share.cloned_agent_id
        return None

    @staticmethod
    def _check_access_allowed(
        session: Session,
        integration: AgentEmailIntegration,
        sender_email: str,
    ) -> bool:
        """Check if sender is allowed based on access mode."""
        # Check allowed_domains first (applies to both modes)
        if integration.allowed_domains:
            domains = [d.strip().lower() for d in integration.allowed_domains.split(",") if d.strip()]
            if domains:
                sender_domain = sender_email.split("@")[-1].lower()
                if sender_domain not in domains:
                    return False

        if integration.access_mode == "open":
            return True

        # Restricted mode
        # Check if user is pre-shared
        user = UserService.get_user_by_email(session=session, email=sender_email)
        if user:
            stmt = select(AgentShare).where(
                AgentShare.original_agent_id == integration.agent_id,
                AgentShare.shared_with_user_id == user.id,
                AgentShare.status.in_([ShareStatus.PENDING, ShareStatus.ACCEPTED]),  # type: ignore
            )
            existing_share = session.exec(stmt).first()
            if existing_share:
                return True

        # Check auto_approve_email_pattern
        if integration.auto_approve_email_pattern:
            if EmailRoutingService._match_email_pattern(
                sender_email, integration.auto_approve_email_pattern
            ):
                return True

        return False

    @staticmethod
    def _match_email_pattern(email: str, pattern_string: str) -> bool:
        """Match email against comma-separated glob patterns (case-insensitive)."""
        email = email.lower()
        patterns = [p.strip().lower() for p in pattern_string.split(",") if p.strip()]
        return any(fnmatch.fnmatch(email, pattern) for pattern in patterns)

    @staticmethod
    def _ensure_user_exists(
        session: Session,
        sender_email: str,
    ) -> uuid.UUID:
        """Ensure a user account exists for the sender email. Creates one if needed."""
        user = UserService.create_email_user(session=session, email=sender_email)
        return user.id

    @staticmethod
    async def _auto_create_share_and_clone(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        clone_share_mode: str,
    ) -> uuid.UUID:
        """Create an auto-share and clone for email integration."""
        from app.services.sharing.agent_share_service import AgentShareService

        share, clone = await AgentShareService.create_auto_share(
            session=session,
            agent_id=agent_id,
            user_id=user_id,
            share_mode=clone_share_mode,
            source=ShareSource.EMAIL_INTEGRATION,
        )
        return clone.id

    @staticmethod
    def _is_clone_ready(
        session: Session,
        clone_agent_id: uuid.UUID,
    ) -> bool:
        """Check if a clone's environment is running and ready."""
        stmt = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == clone_agent_id,
            AgentEnvironment.is_active == True,  # noqa: E712
        )
        env = session.exec(stmt).first()
        if not env:
            return False
        return env.status == "running"

    @staticmethod
    async def _auto_accept_pending_share(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """If user has a pending share for this agent, auto-accept it."""
        from app.services.sharing.agent_share_service import AgentShareService

        stmt = select(AgentShare).where(
            AgentShare.original_agent_id == agent_id,
            AgentShare.shared_with_user_id == user_id,
            AgentShare.status == ShareStatus.PENDING,
        )
        share = session.exec(stmt).first()
        if not share:
            return None

        # Accept the share (creates clone)
        clone = await AgentShareService.accept_share(
            session=session,
            share_id=share.id,
            recipient_id=user_id,
        )

        # Mark share source as email_integration
        share = session.get(AgentShare, share.id)
        if share:
            share.source = ShareSource.EMAIL_INTEGRATION
            session.add(share)
            session.commit()

        return clone.id
