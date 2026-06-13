"""
Admin "LLM Providers" API.

Superuser-only CRUD over **Managed AI Credential** parent records. A parent
record holds the canonical config (name/type/key/base_url/model/defaults) plus
the target user set, and is reconciled into per-user ``AICredential`` child rows
(one per target user). The children look exactly like today's admin-managed
rows to the rest of the system.

All routes require an authenticated superuser (403 for anyone else). Mutations
emit ``SecurityEvent`` records with NO key material:
- per child mutation → keyed to the child owner
  (``admin.ai_credential.provision|update|delete|set_default``)
- per parent batch → keyed to the admin
  (``admin.managed_ai_credential.create|update|delete``)
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.credentials.ai_credential import (
    AICredentialData,
    AICredentialTestRequest,
    AICredentialTestResult,
)
from app.models.credentials.managed_ai_credential import (
    ManagedAICredentialCreate,
    ManagedAICredentialPublic,
    ManagedAICredentialReconcileResult,
    ManagedAICredentialUpdate,
)
from app.models.events.security_event import SecurityEventCreate
from app.services.credentials import model_discovery_service
from app.services.credentials.managed_ai_credentials_service import (
    managed_ai_credentials_service,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/llm-providers", tags=["admin-llm-providers"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


async def _emit_reconcile_events(
    session: SessionDep,
    admin: User,
    result: ManagedAICredentialReconcileResult,
    parent_event_type: str,
) -> None:
    """Emit per-child SecurityEvents (keyed to each affected owner) for the
    added/removed/updated members of a reconcile, plus a parent-level batch
    event keyed to the admin. NEVER includes key material."""
    record = result.record

    for member in result.added:
        await SecurityEventService.create_event(
            session=session,
            user_id=member.user_id,
            data=SecurityEventCreate(
                event_type="admin.ai_credential.provision",
                severity="medium",
                details={
                    "managed_credential_id": str(record.id),
                    "child_credential_id": str(member.child_credential_id),
                    "target_user_id": str(member.user_id),
                    "managed_by_id": str(admin.id),
                },
            ),
        )

    for owner_id in result.removed:
        await SecurityEventService.create_event(
            session=session,
            user_id=owner_id,
            data=SecurityEventCreate(
                event_type="admin.ai_credential.delete",
                severity="medium",
                details={
                    "managed_credential_id": str(record.id),
                    "target_user_id": str(owner_id),
                    "managed_by_id": str(admin.id),
                },
            ),
        )

    for member in result.updated:
        # Only members whose child row was actually mutated this reconcile —
        # a no-op PATCH emits zero update events.
        await SecurityEventService.create_event(
            session=session,
            user_id=member.user_id,
            data=SecurityEventCreate(
                event_type="admin.ai_credential.update",
                severity="medium",
                details={
                    "managed_credential_id": str(record.id),
                    "child_credential_id": str(member.child_credential_id),
                    "target_user_id": str(member.user_id),
                    "managed_by_id": str(admin.id),
                },
            ),
        )

    await SecurityEventService.create_event(
        session=session,
        user_id=admin.id,
        data=SecurityEventCreate(
            event_type=parent_event_type,
            severity="medium",
            details={
                "managed_credential_id": str(record.id),
                "managed_by_id": str(admin.id),
                "type": record.type.value
                if hasattr(record.type, "value")
                else str(record.type),
                "added_count": len(result.added),
                "removed_count": len(result.removed),
                "updated_count": result.updated_count,
                "skipped_count": len(result.skipped),
                "blocked_count": len(result.blocked),
            },
        ),
    )


@router.post("/", response_model=ManagedAICredentialReconcileResult)
async def create_managed_ai_credential(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: ManagedAICredentialCreate,
) -> Any:
    """Create a managed AI credential parent record and reconcile to create one
    child per valid target user.

    Invalid/inactive targets are returned in ``skipped`` rather than failing the
    call. A bad per-type payload (e.g. ``openai_compatible`` without
    base_url/model) fails the whole call with 400.
    """
    result = managed_ai_credentials_service.create(
        session, current_user, data
    )
    await _emit_reconcile_events(
        session, current_user, result, "admin.managed_ai_credential.create"
    )
    return result


@router.get("/", response_model=list[ManagedAICredentialPublic])
def list_managed_ai_credentials(
    session: SessionDep,
    current_user: SuperUser,
    managed_by_id: uuid.UUID | None = Query(
        default=None, description="Filter to records managed by this admin"
    ),
    target_user_id: uuid.UUID | None = Query(
        default=None, description="Filter to records that have this user as a member"
    ),
) -> Any:
    """List managed AI credential parent records fleet-wide, optionally filtered
    by managing admin and/or member user."""
    return managed_ai_credentials_service.list(
        session, current_user, managed_by_id, target_user_id
    )


@router.get("/{managed_credential_id}", response_model=ManagedAICredentialPublic)
def get_managed_ai_credential(
    session: SessionDep,
    current_user: SuperUser,
    managed_credential_id: uuid.UUID,
) -> Any:
    """Get a single managed AI credential parent record (404 if not found)."""
    return managed_ai_credentials_service.get(
        session, current_user, managed_credential_id
    )


@router.patch(
    "/{managed_credential_id}",
    response_model=ManagedAICredentialReconcileResult,
)
async def update_managed_ai_credential(
    *,
    session: SessionDep,
    current_user: SuperUser,
    managed_credential_id: uuid.UUID,
    data: ManagedAICredentialUpdate,
    force: bool = False,
) -> Any:
    """Update a managed AI credential parent record + membership, then reconcile.

    Omitting ``api_key`` keeps the stored key. Omitting ``target_user_ids``
    leaves membership unchanged. ``force`` overrides the Tier-2 blast-radius
    gate on removed members.
    """
    result = managed_ai_credentials_service.update(
        session, current_user, managed_credential_id, data, force=force
    )
    await _emit_reconcile_events(
        session, current_user, result, "admin.managed_ai_credential.update"
    )
    return result


@router.delete("/{managed_credential_id}")
async def delete_managed_ai_credential(
    session: SessionDep,
    current_user: SuperUser,
    managed_credential_id: uuid.UUID,
    force: bool = False,
) -> Message:
    """Delete a managed AI credential parent record + all its children.

    When one or more child removals are blocked by the Tier-2 bundle
    blast-radius gate and ``force`` is not passed, responds 409 with the list of
    blocked members (and their impact) and leaves the record intact.
    """
    result = managed_ai_credentials_service.delete(
        session, current_user, managed_credential_id, force=force
    )

    if result.blocked and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "One or more members could not be removed because their "
                    "credential is in use by a published bundle."
                ),
                "blocked": [b.model_dump(mode="json") for b in result.blocked],
            },
        )

    await _emit_reconcile_events(
        session, current_user, result, "admin.managed_ai_credential.delete"
    )
    return Message(message="Managed AI credential deleted successfully")


@router.post(
    "/{managed_credential_id}/set-default",
    response_model=ManagedAICredentialPublic,
)
async def set_managed_ai_credential_default(
    session: SessionDep,
    current_user: SuperUser,
    managed_credential_id: uuid.UUID,
) -> Any:
    """Set every member's child as that user's default for the type."""
    record = managed_ai_credentials_service.set_default_all(
        session, current_user, managed_credential_id
    )

    for member in record.members:
        await SecurityEventService.create_event(
            session=session,
            user_id=member.user_id,
            data=SecurityEventCreate(
                event_type="admin.ai_credential.set_default",
                severity="medium",
                details={
                    "managed_credential_id": str(record.id),
                    "child_credential_id": str(member.child_credential_id),
                    "target_user_id": str(member.user_id),
                    "managed_by_id": str(current_user.id),
                },
            ),
        )

    return record


@router.post("/test-connection", response_model=AICredentialTestResult)
async def test_managed_ai_credential_connection(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: AICredentialTestRequest,
    managed_credential_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "When set and api_key is blank, resolve the stored parent key for "
            "the probe (Edit-with-blank-key case)."
        ),
    ),
) -> Any:
    """Validate an AI credential before save.

    Key resolution mirrors the user-facing test path, with a parent-aware
    addition: when ``api_key`` is blank and ``managed_credential_id`` is given,
    the stored parent key is decrypted for the probe. The probe never persists
    onto a parent row (managed parents have no per-row discovery cache); it just
    returns the result.
    """
    resolved = AICredentialTestRequest(
        type=data.type,
        api_key=data.api_key,
        base_url=data.base_url,
        credential_id=None,
    )

    if not (data.api_key or "").strip() and managed_credential_id is not None:
        key: AICredentialData = managed_ai_credentials_service.resolve_test_key(
            session, managed_credential_id
        )
        resolved = AICredentialTestRequest(
            type=data.type,
            api_key=key.api_key,
            base_url=data.base_url or key.base_url,
            credential_id=None,
        )

    return await model_discovery_service.test_connection(
        session, current_user.id, resolved
    )
