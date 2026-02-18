import asyncio
import logging
import uuid
from datetime import datetime

from sqlmodel import Session, select, func

from app.models.agent import Agent
from app.models.agent_email_integration import (
    AgentEmailIntegration,
    AgentEmailIntegrationCreate,
    AgentEmailIntegrationUpdate,
    AgentEmailIntegrationPublic,
    ProcessEmailsResult,
)
from app.models.agent_share import AgentShare
from app.models.mail_server_config import MailServerConfig, MailServerType

logger = logging.getLogger(__name__)


class EmailIntegrationService:

    @staticmethod
    def get_email_integration(
        session: Session,
        agent_id: uuid.UUID,
    ) -> AgentEmailIntegration | None:
        statement = select(AgentEmailIntegration).where(
            AgentEmailIntegration.agent_id == agent_id
        )
        return session.exec(statement).first()

    @staticmethod
    def get_email_integration_public(
        session: Session,
        agent_id: uuid.UUID,
    ) -> AgentEmailIntegrationPublic | None:
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            return None
        clone_count = EmailIntegrationService.get_email_clone_count(session, agent_id)
        return EmailIntegrationService._to_public(integration, clone_count)

    @staticmethod
    def create_email_integration(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentEmailIntegrationCreate,
    ) -> AgentEmailIntegration:
        # Validate agent is not a clone
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.is_clone:
            raise ValueError("Cannot create email integration on a clone agent")
        if agent.owner_id != user_id:
            raise ValueError("Not authorized")

        # Check no existing integration
        existing = EmailIntegrationService.get_email_integration(session, agent_id)
        if existing:
            raise ValueError("Email integration already exists for this agent")

        # Validate server references
        EmailIntegrationService._validate_server_refs(session, user_id, data)

        integration = AgentEmailIntegration(
            agent_id=agent_id,
            **data.model_dump(),
        )
        session.add(integration)
        session.commit()
        session.refresh(integration)
        return integration

    @staticmethod
    def update_email_integration(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentEmailIntegrationUpdate,
    ) -> AgentEmailIntegration:
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            raise ValueError("Email integration not found")

        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Not authorized")

        # Validate server references if being changed
        update_dict = data.model_dump(exclude_unset=True)

        if "incoming_server_id" in update_dict and update_dict["incoming_server_id"]:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, update_dict["incoming_server_id"], MailServerType.IMAP
            )
        if "outgoing_server_id" in update_dict and update_dict["outgoing_server_id"]:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, update_dict["outgoing_server_id"], MailServerType.SMTP
            )

        integration.sqlmodel_update(update_dict)
        integration.updated_at = datetime.utcnow()
        session.add(integration)
        session.commit()
        session.refresh(integration)
        return integration

    @staticmethod
    def enable_email_integration(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentEmailIntegration:
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            raise ValueError("Email integration not found")

        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Not authorized")
        if agent.is_clone:
            raise ValueError("Cannot enable email integration on a clone agent")

        # Validate required fields are configured
        EmailIntegrationService._validate_for_enable(session, integration, user_id)

        integration.enabled = True
        integration.updated_at = datetime.utcnow()
        session.add(integration)
        session.commit()
        session.refresh(integration)
        return integration

    @staticmethod
    def disable_email_integration(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentEmailIntegration:
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            raise ValueError("Email integration not found")

        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Not authorized")

        integration.enabled = False
        integration.updated_at = datetime.utcnow()
        session.add(integration)
        session.commit()
        session.refresh(integration)
        return integration

    @staticmethod
    def delete_email_integration(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            raise ValueError("Email integration not found")

        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Not authorized")

        session.delete(integration)
        session.commit()

    @staticmethod
    async def process_emails(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ProcessEmailsResult:
        """
        Manually trigger email polling and processing for an agent.

        Polls the configured IMAP mailbox for new emails, then processes
        any that match expected patterns into agent sessions.
        Also retries any previously pending emails.
        """
        from app.services.email.polling_service import EmailPollingService
        from app.services.email.processing_service import EmailProcessingService

        # Validate ownership and integration state
        integration = EmailIntegrationService.get_email_integration(session, agent_id)
        if not integration:
            raise ValueError("Email integration not configured")
        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Not authorized")
        if not integration.enabled:
            raise ValueError("Email integration is not enabled")

        result = ProcessEmailsResult()

        # Step 1: Poll IMAP mailbox for new emails (blocking I/O - run in thread pool)
        stored_ids = await asyncio.to_thread(
            EmailPollingService.poll_agent_mailbox, session, agent_id
        )
        result.polled = len(stored_ids)

        # Step 2: Process newly polled emails
        for email_id in stored_ids:
            try:
                success = await EmailProcessingService.process_incoming_email(session, email_id)
                if success:
                    result.processed += 1
                else:
                    result.pending += 1
            except Exception as e:
                logger.error(
                    f"Agent {agent_id}: failed to process email {email_id}: {e}",
                    exc_info=True,
                )
                result.errors += 1

        # Step 3: Retry any previously pending emails
        try:
            retried = await EmailProcessingService.process_pending_emails(session)
            result.processed += retried
        except Exception as e:
            logger.error(
                f"Agent {agent_id}: failed to process pending emails: {e}",
                exc_info=True,
            )

        # Build summary message
        parts = []
        if result.polled > 0:
            parts.append(f"{result.polled} email(s) fetched")
        if result.processed > 0:
            parts.append(f"{result.processed} processed into sessions")
        if result.pending > 0:
            parts.append(f"{result.pending} pending (clone not ready)")
        if result.errors > 0:
            parts.append(f"{result.errors} error(s)")
        if not parts:
            parts.append("No new emails found")
        result.message = ". ".join(parts)

        return result

    @staticmethod
    def get_email_clone_count(
        session: Session,
        agent_id: uuid.UUID,
    ) -> int:
        """Count clones created via email integration for this agent."""
        from app.models.agent_share import ShareSource, ShareStatus
        statement = (
            select(func.count())
            .select_from(AgentShare)
            .where(
                AgentShare.original_agent_id == agent_id,
                AgentShare.source == ShareSource.EMAIL_INTEGRATION,
                AgentShare.status == ShareStatus.ACCEPTED,
            )
        )
        return session.exec(statement).one()

    @staticmethod
    def _validate_server_refs(
        session: Session,
        user_id: uuid.UUID,
        data: AgentEmailIntegrationCreate,
    ) -> None:
        if data.incoming_server_id:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, data.incoming_server_id, MailServerType.IMAP
            )
        if data.outgoing_server_id:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, data.outgoing_server_id, MailServerType.SMTP
            )

    @staticmethod
    def _validate_server_ownership(
        session: Session,
        user_id: uuid.UUID,
        server_id: uuid.UUID,
        expected_type: MailServerType,
    ) -> None:
        server = session.get(MailServerConfig, server_id)
        if not server:
            raise ValueError(f"Mail server {server_id} not found")
        if server.user_id != user_id:
            raise ValueError(f"Mail server {server_id} does not belong to you")
        if server.server_type != expected_type:
            raise ValueError(
                f"Mail server {server_id} is {server.server_type.value}, expected {expected_type.value}"
            )

    @staticmethod
    def _validate_for_enable(
        session: Session,
        integration: AgentEmailIntegration,
        user_id: uuid.UUID,
    ) -> None:
        errors = []
        if not integration.incoming_server_id:
            errors.append("Incoming mail server is required")
        else:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, integration.incoming_server_id, MailServerType.IMAP
            )
        if not integration.incoming_mailbox:
            errors.append("Incoming mailbox address is required")
        if not integration.outgoing_server_id:
            errors.append("Outgoing mail server is required")
        else:
            EmailIntegrationService._validate_server_ownership(
                session, user_id, integration.outgoing_server_id, MailServerType.SMTP
            )
        if not integration.outgoing_from_address:
            errors.append("Outgoing from address is required")

        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _to_public(
        integration: AgentEmailIntegration,
        email_clone_count: int = 0,
    ) -> AgentEmailIntegrationPublic:
        return AgentEmailIntegrationPublic(
            id=integration.id,
            agent_id=integration.agent_id,
            enabled=integration.enabled,
            access_mode=integration.access_mode,
            auto_approve_email_pattern=integration.auto_approve_email_pattern,
            allowed_domains=integration.allowed_domains,
            max_clones=integration.max_clones,
            clone_share_mode=integration.clone_share_mode,
            agent_session_mode=integration.agent_session_mode,
            incoming_server_id=integration.incoming_server_id,
            incoming_mailbox=integration.incoming_mailbox,
            outgoing_server_id=integration.outgoing_server_id,
            outgoing_from_address=integration.outgoing_from_address,
            email_clone_count=email_clone_count,
            created_at=integration.created_at,
            updated_at=integration.updated_at,
        )
