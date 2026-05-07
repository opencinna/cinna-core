"""
Email Routing Service - Maps sender email to the correct install agent.

Phase 2 — clones are gone. The "clone" terminology in this file's external
contract (mode strings, public function names) is preserved for backwards
compatibility with the email integration model and the existing UI labels;
internally we look up / create per-user **installs** of the publisher's
bundle via ``InstallService``.

Owner mode: sessions live on the publisher's working install. Sender email
is captured on the session so the platform can reply.

Clone mode (default): each sender gets their own per-user install of the
agent's bundle, with a separate App Data volume keyed on (user, bundle).
"""
import fnmatch
import logging
import uuid

from sqlmodel import Session, select

from app.models.agents.agent import Agent
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
          - target_agent_id: install (clone mode) or the publisher install itself (owner mode)
          - is_ready: whether the target agent's environment is running
          - session_mode: "clone" or "owner"
        Raises: EmailAccessDenied if sender is not allowed.
        """
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration or not integration.enabled:
            raise EmailAccessDenied("Email integration is not enabled for this agent")

        sender_email = sender_email.strip().lower()

        # Owner mode: route directly to the publisher install (no per-user install)
        if integration.agent_session_mode == AgentSessionMode.OWNER:
            # Access control still applies in owner mode
            if not EmailRoutingService._check_access_allowed(session, integration, sender_email):
                raise EmailAccessDenied(
                    f"Email from {sender_email} is not allowed for this agent"
                )
            is_ready = EmailRoutingService._is_clone_ready(session, agent_id)
            return agent_id, is_ready, AgentSessionMode.OWNER

        # Install (clone) mode (default): each sender gets their own install
        # 1. Check for existing install
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
                f"Maximum email install limit ({integration.max_clones}) reached"
            )

        # 4. Ensure user exists
        user_id = EmailRoutingService._ensure_user_exists(session, sender_email)

        # 5. Auto-install the bundle for this user
        clone_id = await EmailRoutingService._auto_install_bundle(
            session, agent_id, user_id
        )
        is_ready = EmailRoutingService._is_clone_ready(session, clone_id)
        return clone_id, is_ready, AgentSessionMode.CLONE

    @staticmethod
    def _find_existing_clone(
        session: Session,
        agent_id: uuid.UUID,
        sender_email: str,
    ) -> uuid.UUID | None:
        """Find an existing per-user install of the same bundle for the sender."""
        user = UserService.get_user_by_email(session=session, email=sender_email)
        if not user:
            return None

        publisher_install = session.get(Agent, agent_id)
        if not publisher_install:
            return None

        # Same bundle, same recipient — that's the user's install row.
        stmt = select(Agent).where(
            Agent.bundle_id == publisher_install.bundle_id,
            Agent.owner_id == user.id,
        )
        existing = session.exec(stmt).first()
        return existing.id if existing else None

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

        # Restricted mode — for the bundle world we treat any pre-existing
        # install of this bundle (catalog or grant) as "pre-approved", which
        # mirrors today's "user has a pending/accepted share" semantics.
        user = UserService.get_user_by_email(session=session, email=sender_email)
        if user:
            publisher_install = session.get(Agent, integration.agent_id)
            if publisher_install:
                stmt = select(Agent).where(
                    Agent.bundle_id == publisher_install.bundle_id,
                    Agent.owner_id == user.id,
                )
                if session.exec(stmt).first():
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
    async def _auto_install_bundle(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> uuid.UUID:
        """Auto-install the publisher's bundle for ``user_id``.

        If the publisher install hasn't been published yet (no ``AgentBundle``
        row), we still need to give the email sender their own install. The
        ``InstallService.install_bundle_for_email`` helper handles that case
        by lazily promoting the agent into a bundle.
        """
        from app.services.bundles.install_service import InstallService

        install = await InstallService.install_bundle_for_email(
            session=session,
            publisher_agent_id=agent_id,
            recipient_user_id=user_id,
        )
        return install.id

    @staticmethod
    def _is_clone_ready(
        session: Session,
        clone_agent_id: uuid.UUID,
    ) -> bool:
        """Check if an install's environment is running and ready."""
        stmt = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == clone_agent_id,
            AgentEnvironment.is_active == True,  # noqa: E712
        )
        env = session.exec(stmt).first()
        if not env:
            return False
        return env.status == "running"
