"""
AI Credentials API Routes

CRUD endpoints for managing named AI credentials.
"""
import uuid
from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.models import Message
from app.models.credentials.ai_credential import (
    AICredentialCreate,
    AICredentialUpdate,
    AICredentialPublic,
    AICredentialsPublic,
    AffectedEnvironmentsPublic,
)
from app.services.credentials.ai_credentials_service import ai_credentials_service

router = APIRouter(prefix="/ai-credentials", tags=["ai-credentials"])


@router.get("/", response_model=AICredentialsPublic)
def list_ai_credentials(
    session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    List all AI credentials for the current user.
    """
    credentials = ai_credentials_service.list_credentials(session, current_user.id)
    return AICredentialsPublic(data=credentials, count=len(credentials))


@router.get("/resolve-default/{sdk_engine}", response_model=AICredentialPublic | None)
def resolve_default_credential(
    session: SessionDep,
    current_user: CurrentUser,
    sdk_engine: str,
) -> Any:
    """
    Resolve the best default credential for a given SDK engine.
    Returns the credential that would be used when 'Use Default' is selected,
    or null if no matching default credential exists.
    """
    return ai_credentials_service.resolve_default_credential_for_sdk(
        session, current_user.id, sdk_engine
    )


@router.get("/{credential_id}", response_model=AICredentialPublic)
def get_ai_credential(
    session: SessionDep, current_user: CurrentUser, credential_id: uuid.UUID
) -> Any:
    """
    Get a single AI credential by ID.
    """
    credential = ai_credentials_service.get_credential(
        session, credential_id, current_user.id
    )
    return ai_credentials_service._to_public(credential, session)


@router.post("/", response_model=AICredentialPublic)
def create_ai_credential(
    *, session: SessionDep, current_user: CurrentUser, data: AICredentialCreate
) -> Any:
    """
    Create a new AI credential.
    """
    return ai_credentials_service.create_credential(session, current_user.id, data)


@router.patch("/{credential_id}", response_model=AICredentialPublic)
def update_ai_credential(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
    data: AICredentialUpdate,
) -> Any:
    """
    Update an existing AI credential.
    """
    return ai_credentials_service.update_credential(
        session, credential_id, current_user.id, data
    )


@router.delete("/{credential_id}")
def delete_ai_credential(
    session: SessionDep, current_user: CurrentUser, credential_id: uuid.UUID
) -> Message:
    """
    Delete an AI credential.
    """
    ai_credentials_service.delete_credential(session, credential_id, current_user.id)
    return Message(message="AI credential deleted successfully")


@router.post("/{credential_id}/set-default", response_model=AICredentialPublic)
def set_ai_credential_default(
    session: SessionDep, current_user: CurrentUser, credential_id: uuid.UUID
) -> Any:
    """
    Set an AI credential as the default for its type.
    This also syncs the credential to the user's profile for backward compatibility.
    """
    return ai_credentials_service.set_default(session, credential_id, current_user.id)


@router.get("/{credential_id}/affected-environments", response_model=AffectedEnvironmentsPublic)
def get_affected_environments(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID
) -> Any:
    """
    Get all agent-environments that use this AI credential.
    Includes information about users with access via shares.
    """
    return ai_credentials_service.get_affected_environments(
        session, credential_id, current_user.id
    )
