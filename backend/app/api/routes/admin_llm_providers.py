"""
Admin "LLM Providers" API.

Superuser-only CRUD for provisioning AI credentials on behalf of users
(per-user provisioning, NOT key sharing). Each provisioned row is owned by its
target user and marked ``is_admin_managed`` so it is read-only through the
user-facing AI-credentials CRUD.

All routes require an authenticated superuser (403 for anyone else). Mutations
emit a ``SecurityEvent`` with NO key material.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.credentials.ai_credential import (
    AICredentialUpdate,
    AdminAICredentialCreate,
    AdminAICredentialProvisionResult,
    AdminAICredentialPublic,
)
from app.models.events.security_event import SecurityEventCreate
from app.services.credentials.admin_ai_credentials_service import (
    admin_ai_credentials_service,
)
from app.services.credentials.ai_credentials_service import (
    AICredentialInUseError,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/llm-providers", tags=["admin-llm-providers"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.post("/", response_model=AdminAICredentialProvisionResult)
async def provision_ai_credentials(
    *, session: SessionDep, current_user: SuperUser, data: AdminAICredentialCreate
) -> Any:
    """Provision an AI credential for one or more target users.

    Creates one admin-managed credential per valid target. Invalid/inactive
    targets are returned in ``skipped`` rather than failing the call. A bad
    per-type payload (e.g. ``openai_compatible`` without base_url/model) fails
    the whole call with 400.
    """
    result = admin_ai_credentials_service.provision_for_users(
        session, current_user, data
    )

    # One SecurityEvent per provisioned row (OQ-7) — NO key material.
    for created in result.created:
        await SecurityEventService.create_event(
            session=session,
            user_id=created.owner_id,
            data=SecurityEventCreate(
                event_type="admin.ai_credential.provision",
                severity="medium",
                details={
                    "credential_id": str(created.id),
                    "target_user_id": str(created.owner_id),
                    "type": created.type.value
                    if hasattr(created.type, "value")
                    else str(created.type),
                    "managed_by_id": str(current_user.id),
                    "set_as_default": data.set_as_default,
                    "set_user_sdk_defaults": data.set_user_sdk_defaults,
                },
            ),
        )

    # Batch summary event (OQ-7).
    await SecurityEventService.create_event(
        session=session,
        user_id=current_user.id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.provision_batch",
            severity="medium",
            details={
                "managed_by_id": str(current_user.id),
                "type": data.type.value
                if hasattr(data.type, "value")
                else str(data.type),
                "created_count": len(result.created),
                "skipped_count": len(result.skipped),
                "created_credential_ids": [str(c.id) for c in result.created],
                "target_user_ids": [str(c.owner_id) for c in result.created],
            },
        ),
    )

    return result


@router.get("/", response_model=list[AdminAICredentialPublic])
def list_managed_ai_credentials(
    session: SessionDep,
    current_user: SuperUser,
    target_user_id: uuid.UUID | None = Query(
        default=None, description="Optional filter to a single target user"
    ),
) -> Any:
    """List admin-managed AI credentials fleet-wide, optionally scoped to a
    target user."""
    return admin_ai_credentials_service.list_managed(
        session, current_user, target_user_id
    )


@router.get("/{credential_id}", response_model=AdminAICredentialPublic)
def get_managed_ai_credential(
    session: SessionDep, current_user: SuperUser, credential_id: uuid.UUID
) -> Any:
    """Get a single admin-managed AI credential (404 if missing or not
    admin-managed)."""
    return admin_ai_credentials_service.get_managed(
        session, current_user, credential_id
    )


@router.patch("/{credential_id}", response_model=AdminAICredentialPublic)
async def update_managed_ai_credential(
    *,
    session: SessionDep,
    current_user: SuperUser,
    credential_id: uuid.UUID,
    data: AICredentialUpdate,
) -> Any:
    """Update an admin-managed AI credential on behalf of its owner."""
    result = admin_ai_credentials_service.update_managed(
        session, current_user, credential_id, data
    )
    await SecurityEventService.create_event(
        session=session,
        user_id=result.owner_id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.update",
            severity="medium",
            details={
                "credential_id": str(result.id),
                "target_user_id": str(result.owner_id),
                "managed_by_id": str(current_user.id),
            },
        ),
    )
    return result


@router.delete("/{credential_id}")
async def delete_managed_ai_credential(
    session: SessionDep,
    current_user: SuperUser,
    credential_id: uuid.UUID,
    force: bool = False,
) -> Message:
    """Delete an admin-managed AI credential.

    Blocked with HTTP 409 when one or more published bundles reference it as a
    publisher-provided AI credential, unless ``force`` is passed.
    """
    # Capture the owner before deletion for the audit event.
    existing = admin_ai_credentials_service.get_managed(
        session, current_user, credential_id
    )
    try:
        admin_ai_credentials_service.delete_managed(
            session, current_user, credential_id, force=force
        )
    except AICredentialInUseError as e:
        raise HTTPException(
            status_code=409, detail=e.impact.model_dump(mode="json")
        )
    await SecurityEventService.create_event(
        session=session,
        user_id=existing.owner_id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.delete",
            severity="medium",
            details={
                "credential_id": str(credential_id),
                "target_user_id": str(existing.owner_id),
                "managed_by_id": str(current_user.id),
                "force": force,
            },
        ),
    )
    return Message(message="Admin-managed AI credential deleted successfully")


@router.post("/{credential_id}/set-default", response_model=AdminAICredentialPublic)
async def set_managed_ai_credential_default(
    session: SessionDep, current_user: SuperUser, credential_id: uuid.UUID
) -> Any:
    """Set an admin-managed AI credential as the owner-user's default for its
    type."""
    result = admin_ai_credentials_service.set_managed_default(
        session, current_user, credential_id
    )
    await SecurityEventService.create_event(
        session=session,
        user_id=result.owner_id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.set_default",
            severity="medium",
            details={
                "credential_id": str(result.id),
                "target_user_id": str(result.owner_id),
                "managed_by_id": str(current_user.id),
            },
        ),
    )
    return result
