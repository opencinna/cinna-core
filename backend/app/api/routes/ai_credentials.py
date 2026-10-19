"""
AI Credentials API Routes

CRUD endpoints for managing named AI credentials.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import Message
from app.models.credentials.ai_credential import (
    AICredentialCreate,
    AICredentialUpdate,
    AICredentialPublic,
    AICredentialsPublic,
    AICredentialDeletionImpact,
    AICredentialTestRequest,
    AICredentialTestResult,
    AffectedEnvironmentsPublic,
)
from app.models.credentials.ai_credential_group_share import (
    AICredentialGroupShareCreate,
    AICredentialGroupSharePublic,
    AICredentialGroupSharesPublic,
)
from app.models.credentials.ai_credential_share import SharedAICredentialsPublic
from app.models.credentials.managed_ai_credential_membership import (
    UserKeyProvisioningPublic,
)
from app.services.credentials.ai_credentials_service import (
    ai_credentials_service,
    AICredentialInUseError,
)
from app.services.credentials import model_discovery_service
from app.services.credentials.key_provisioning_service import (
    key_provisioning_service,
)

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


@router.get("/provisioning", response_model=list[UserKeyProvisioningPublic])
def list_my_key_provisionings(
    session: SessionDep, current_user: CurrentUser
) -> Any:
    """Keys an administrator is having created for this user, and the failures.

    Deliberately a **separate** list from ``GET /ai-credentials/``: that one is
    credentials, and every credential in it is usable. A member of a minted
    record holds no credential until the mint lands, so their state has nowhere
    to live there and lives here instead.

    Placed above ``/{credential_id}`` so the literal path is matched before the
    UUID route claims it.
    """
    return key_provisioning_service.list_user_provisionings(
        session, current_user.id
    )


@router.get("/shared-with-me", response_model=SharedAICredentialsPublic)
def list_ai_credentials_shared_with_me(
    session: SessionDep, current_user: CurrentUser
) -> Any:
    """AI credentials other people share with you, directly or through a user group.

    Declared above ``/{credential_id}`` so the literal path wins.
    """
    credentials = ai_credentials_service.list_shared_with_me(session, current_user.id)
    return SharedAICredentialsPublic(data=credentials, count=len(credentials))


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


@router.post("/test-connection", response_model=AICredentialTestResult)
async def test_ai_credential_connection(
    *, session: SessionDep, current_user: CurrentUser, data: AICredentialTestRequest
) -> Any:
    """
    Validate an AI credential and refresh its discovered model list.

    Tests that the key actually works against the provider's native model
    endpoint. The key comes from ``api_key`` (Add form) or, when
    ``credential_id`` is set, the stored credential (Edit) — owner-scoped.

    For the Edit case (``credential_id`` present) the fresh model list is
    persisted onto the credential row (force-refresh). Returns
    ``AICredentialTestResult``: ``success`` + ``models`` on a working key;
    ``success=True`` with an ``error`` skip-reason when listing isn't supported
    for the credential type/token; ``success=False`` with ``error="invalid_key"``
    on a provider auth rejection.
    """
    return await model_discovery_service.test_connection(
        session, current_user.id, data
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


@router.get(
    "/{credential_id}/deletion-impact",
    response_model=AICredentialDeletionImpact,
)
def get_ai_credential_deletion_impact(
    session: SessionDep, current_user: CurrentUser, credential_id: uuid.UUID
) -> Any:
    """Classify the blast radius of deleting this AI credential.

    Returns Tier 2 (with affected bundles) when one or more published bundles
    reference it as a publisher-provided AI credential, otherwise Tier 0.
    """
    return ai_credentials_service.get_deletion_impact(
        session, credential_id, current_user.id
    )


@router.delete("/{credential_id}")
def delete_ai_credential(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
    force: bool = False,
) -> Message:
    """
    Delete an AI credential.

    Blocked with HTTP 409 when one or more published bundles reference it as a
    publisher-provided AI credential, unless ``force`` is passed.
    """
    try:
        ai_credentials_service.delete_credential(
            session, credential_id, current_user.id, force=force
        )
    except AICredentialInUseError as e:
        raise HTTPException(
            status_code=409, detail=e.impact.model_dump(mode="json")
        )
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


# ── Group shares (user groups) ───────────────────────────────────────────────


@router.post(
    "/{credential_id}/group-shares", response_model=AICredentialGroupSharePublic
)
def share_ai_credential_with_group(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
    body: AICredentialGroupShareCreate,
) -> Any:
    """Share an AI credential you own with a user group.

    Members can then choose it in their environments' AI configuration.
    Admin-provisioned keys cannot be shared (400).
    """
    return ai_credentials_service.share_with_group(
        session, credential_id, current_user.id, body.group_id
    )


@router.get(
    "/{credential_id}/group-shares", response_model=AICredentialGroupSharesPublic
)
def list_ai_credential_group_shares(
    session: SessionDep, current_user: CurrentUser, credential_id: uuid.UUID
) -> Any:
    """The user groups an AI credential you own is shared with."""
    shares = ai_credentials_service.list_group_shares(
        session, credential_id, current_user.id
    )
    return AICredentialGroupSharesPublic(data=shares, count=len(shares))


@router.delete("/{credential_id}/group-shares/{group_id}", response_model=Message)
async def revoke_ai_credential_group_share(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
    group_id: uuid.UUID,
) -> Message:
    """Stop sharing an AI credential with a user group.

    Members with no other path to it have it cleared from their environments,
    and the running ones are restarted so the key leaves the container.
    """
    await ai_credentials_service.revoke_group_share(
        session, credential_id, current_user.id, group_id
    )
    return Message(message="Group share revoked successfully")
