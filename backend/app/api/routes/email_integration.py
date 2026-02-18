import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    AgentEmailIntegrationCreate,
    AgentEmailIntegrationUpdate,
    AgentEmailIntegrationPublic,
    ProcessEmailsResult,
    Message,
)
from app.services.email.integration_service import EmailIntegrationService

router = APIRouter(prefix="/agents", tags=["email-integration"])


def _check_agent_owner(session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
    """Verify agent exists and user is the owner."""
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return agent


@router.get("/{agent_id}/email-integration", response_model=AgentEmailIntegrationPublic | None)
def get_email_integration(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """Get email integration configuration for an agent."""
    _check_agent_owner(session, agent_id, current_user.id)
    return EmailIntegrationService.get_email_integration_public(session, agent_id)


@router.post("/{agent_id}/email-integration", response_model=AgentEmailIntegrationPublic)
def create_or_update_email_integration(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    config_in: AgentEmailIntegrationCreate,
) -> Any:
    """Create or update email integration for an agent."""
    try:
        existing = EmailIntegrationService.get_email_integration(session, agent_id)
        if existing:
            # Update existing
            update_data = AgentEmailIntegrationUpdate(**config_in.model_dump())
            integration = EmailIntegrationService.update_email_integration(
                session=session,
                agent_id=agent_id,
                user_id=current_user.id,
                data=update_data,
            )
        else:
            # Create new
            integration = EmailIntegrationService.create_email_integration(
                session=session,
                agent_id=agent_id,
                user_id=current_user.id,
                data=config_in,
            )
        clone_count = EmailIntegrationService.get_email_clone_count(session, agent_id)
        return EmailIntegrationService._to_public(integration, clone_count)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.put("/{agent_id}/email-integration/enable", response_model=AgentEmailIntegrationPublic)
def enable_email_integration(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """Enable email integration for an agent."""
    try:
        integration = EmailIntegrationService.enable_email_integration(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )
        clone_count = EmailIntegrationService.get_email_clone_count(session, agent_id)
        return EmailIntegrationService._to_public(integration, clone_count)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.put("/{agent_id}/email-integration/disable", response_model=AgentEmailIntegrationPublic)
def disable_email_integration(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """Disable email integration for an agent."""
    try:
        integration = EmailIntegrationService.disable_email_integration(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )
        clone_count = EmailIntegrationService.get_email_clone_count(session, agent_id)
        return EmailIntegrationService._to_public(integration, clone_count)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{agent_id}/email-integration")
def delete_email_integration(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Message:
    """Remove email integration from an agent."""
    try:
        EmailIntegrationService.delete_email_integration(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )
        return Message(message="Email integration removed successfully")
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/{agent_id}/email-integration/process-emails", response_model=ProcessEmailsResult)
async def process_emails(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """
    Manually trigger email polling and processing for an agent.

    Polls the configured IMAP mailbox for new emails, then processes
    any that match expected patterns into agent sessions.
    """
    try:
        return await EmailIntegrationService.process_emails(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() or "not configured" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))
