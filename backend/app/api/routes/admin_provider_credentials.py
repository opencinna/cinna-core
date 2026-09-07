"""Admin API for provider administration secrets and provider descriptions.

Two superuser surfaces that belong together because one configures what the other
describes:

- ``/admin/provider-admin-credentials`` — connect a provider organisation, verify
  it, apply its spend limit, disconnect it. **The secret is write-only.** It is
  accepted on create and update, is never a field of any response, and no other
  endpoint can reach the table it lives in.
- ``/admin/provider-adapters`` — what the server knows about each provider. Read
  by the admin UI instead of keeping its own copies of provider names, required
  fields and SDK engines.

Every mutation writes a ``SecurityEvent`` keyed to the acting admin, in the same
namespace as the existing ``admin.ai_credential.*`` events. External key refs are
allowed in event details — they are what makes a key nameable after the fact —
and key material never is.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.credentials.provider_admin_credential import (
    ProviderAdminCredential,
    ProviderAdminCredentialCreate,
    ProviderAdminCredentialPublic,
    ProviderAdminCredentialUpdate,
    ProviderAdminCredentialVerifyResult,
)
from app.models.credentials.provider_adapter import (
    ProviderAdapterPublic,
    ProviderAdaptersPublic,
)
from app.models.events.security_event import SecurityEventCreate
from app.services.ai_providers import registry
from app.services.credentials.provider_admin_credentials_service import (
    ProviderAdminCredentialInUseError,
    provider_admin_credentials_service,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/provider-admin-credentials",
    tags=["admin-provider-credentials"],
)
adapters_router = APIRouter(
    prefix="/admin/provider-adapters", tags=["admin-provider-adapters"]
)

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


async def _audit(
    session: SessionDep,
    admin: User,
    event_type: str,
    details: dict[str, Any],
) -> None:
    """One admin-initiated provisioning act, recorded. Never key material."""
    await SecurityEventService.create_event(
        session=session,
        user_id=admin.id,
        data=SecurityEventCreate(
            event_type=event_type, severity="medium", details=details
        ),
    )


@router.post("/", response_model=ProviderAdminCredentialPublic)
async def create_provider_admin_credential(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: ProviderAdminCredentialCreate,
) -> Any:
    """Connect a provider organisation.

    The secret is stored encrypted and is not returned here or anywhere else.
    Creating the record does not contact the provider — press Verify for that, so
    a provider outage cannot stop an admin from recording the configuration.
    """
    record = provider_admin_credentials_service.create(
        session, current_user, data
    )
    await _audit(
        session,
        current_user,
        "admin.provider_admin_credential.create",
        {
            "provider_admin_credential_id": str(record.id),
            "provider_type": record.provider_type.value
            if hasattr(record.provider_type, "value")
            else str(record.provider_type),
            "project_id": record.config.project_id,
        },
    )
    return record


@router.get("/", response_model=list[ProviderAdminCredentialPublic])
def list_provider_admin_credentials(
    session: SessionDep, current_user: SuperUser
) -> Any:
    """List connected provider organisations. Secrets are never included."""
    return provider_admin_credentials_service.list(session, current_user)


@router.get("/{credential_id}", response_model=ProviderAdminCredentialPublic)
def get_provider_admin_credential(
    session: SessionDep, current_user: SuperUser, credential_id: uuid.UUID
) -> Any:
    return provider_admin_credentials_service.get(
        session, current_user, credential_id
    )


@router.patch("/{credential_id}", response_model=ProviderAdminCredentialPublic)
async def update_provider_admin_credential(
    *,
    session: SessionDep,
    current_user: SuperUser,
    credential_id: uuid.UUID,
    data: ProviderAdminCredentialUpdate,
) -> Any:
    """Update a connected organisation. Omitting ``secret`` keeps the stored one."""
    record = provider_admin_credentials_service.update(
        session, current_user, credential_id, data
    )
    await _audit(
        session,
        current_user,
        "admin.provider_admin_credential.update",
        {
            "provider_admin_credential_id": str(credential_id),
            # Whether the secret was replaced, never what it was replaced with.
            "secret_rotated": data.secret is not None,
        },
    )
    return record


@router.delete("/{credential_id}")
async def delete_provider_admin_credential(
    session: SessionDep,
    current_user: SuperUser,
    credential_id: uuid.UUID,
    force: bool = False,
) -> Message:
    """Disconnect a provider organisation.

    Refused with 409 while managed credentials still mint through it or keys
    minted with it are still live: this secret is the only way to destroy those
    keys, so deleting it strands them at the provider permanently. ``force``
    overrides, and the override is audited with the counts it overrode.
    """
    try:
        minting_count, live_count = provider_admin_credentials_service.delete(
            session, current_user, credential_id, force=force
        )
    except ProviderAdminCredentialInUseError as in_use:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "provider_admin_credential_in_use",
                "message": str(in_use),
                "minting_credential_count": in_use.minting_credential_count,
                "live_minted_key_count": in_use.live_minted_key_count,
            },
        )
    await _audit(
        session,
        current_user,
        "admin.provider_admin_credential.delete",
        {
            "provider_admin_credential_id": str(credential_id),
            "forced": force,
            # What the force overrode, not merely that it was used. This is the
            # one action that permanently strands live provider keys, and its
            # audit row is the last place anyone can learn how many.
            "minting_credential_count": minting_count,
            "live_minted_key_count": live_count,
        },
    )
    return Message(message="Provider admin credential deleted successfully")


@router.post(
    "/{credential_id}/verify", response_model=ProviderAdminCredentialVerifyResult
)
async def verify_provider_admin_credential(
    session: SessionDep, current_user: SuperUser, credential_id: uuid.UUID
) -> Any:
    """Check the secret and the project's spend cap in one press.

    A project carrying no spend limit at all is reported as not capped, and
    minting into it is refused. A limit reporting ``inactive`` is a cap that has
    not yet been hit, not a missing one — ``enforcement.status`` is a runtime
    state and is deliberately not the predicate.
    """
    result = await provider_admin_credentials_service.verify(
        session, current_user, credential_id
    )
    await _audit(
        session,
        current_user,
        "admin.provider_admin_credential.verify",
        {
            "provider_admin_credential_id": str(credential_id),
            "ok": result.ok,
            "spend_limit_enforcing": result.spend_limit_enforcing,
            "error": result.error,
        },
    )
    return result


@adapters_router.get("/", response_model=ProviderAdaptersPublic)
def list_provider_adapters(
    session: SessionDep, current_user: SuperUser
) -> Any:
    """Every provider the server supports, as its adapter declares it.

    Derived from the registry, so a sixth provider appears here the moment its
    adapter is registered — no list to update, and no way for this endpoint to
    disagree with the validation it describes.
    """
    adapters = registry.all_adapters()
    # One query for "which providers have an organisation connected", so the
    # per-adapter answer below is a lookup rather than a query each.
    connected_types = set(
        session.exec(select(ProviderAdminCredential.provider_type).distinct())
    )
    data = [
        ProviderAdapterPublic(
            type=adapter.type,
            label=adapter.label,
            account_config_display_name=adapter.account_config_display_name,
            account_config_slug=adapter.account_config_slug,
            sdk_engine=adapter.sdk_engine,
            requires_base_url=adapter.requires_base_url,
            requires_model=adapter.requires_model,
            supports_model_listing=adapter.supports_model_listing,
            issues_oauth_tokens=adapter.issues_oauth_tokens,
            supports_minting=adapter.supports_minting,
            admin_config_schema=(
                adapter.key_provisioner.config_schema()
                if adapter.key_provisioner is not None
                else None
            ),
            can_mint_now=(
                adapter.supports_minting and adapter.type in connected_types
            ),
        )
        for adapter in adapters
    ]
    return ProviderAdaptersPublic(data=data, count=len(data))
