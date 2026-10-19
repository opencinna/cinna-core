"""
Credential Sharing API Routes.

These routes handle sharing credentials with other users.
"""
from uuid import UUID
from typing import Any

from fastapi import APIRouter, HTTPException, Body

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Credential,
    CredentialShareCreate,
    CredentialSharePublic,
    CredentialSharesPublic,
    SharedCredentialsPublic,
    CredentialPublic,
    Message,
)
from app.models.credentials.credential_group_share import (
    CredentialGroupShareCreate,
    CredentialGroupSharePublic,
    CredentialGroupSharesPublic,
)
from app.services.credentials.credential_share_service import (
    CredentialGroupShareError,
    CredentialShareService,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])


def _credential_to_public(session, credential: Credential) -> CredentialPublic:
    """Convert a Credential model to CredentialPublic with share_count."""
    share_count = CredentialShareService.get_share_count_for_credential(
        session=session, credential_id=credential.id
    )
    return CredentialPublic(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        notes=credential.notes,
        allow_sharing=credential.allow_sharing,
        allow_template_sharing=credential.allow_template_sharing,
        template_private_fields=list(credential.template_private_fields or []),
        owner_id=credential.owner_id,
        user_workspace_id=credential.user_workspace_id,
        share_count=share_count,
        group_share_count=CredentialShareService.get_group_share_count_for_credential(
            session, credential.id
        ),
    )


@router.post("/{credential_id}/shares", response_model=CredentialSharePublic)
def share_credential(
    credential_id: UUID,
    share_data: CredentialShareCreate,
    session: SessionDep,
    current_user: CurrentUser
) -> Any:
    """
    Share a credential with another user (by email).

    - Credential must have allow_sharing=true
    - Target user must exist
    - Cannot share with yourself
    """
    try:
        share = CredentialShareService.share_credential(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
            shared_with_email=share_data.shared_with_email,
            source="direct",  # explicit: a user→user direct share
        )
        return share
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{credential_id}/shares", response_model=CredentialSharesPublic)
def get_credential_shares(
    credential_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> Any:
    """
    Get all shares for a credential you own.
    """
    try:
        shares = CredentialShareService.get_shares_by_credential(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id
        )
        return CredentialSharesPublic(data=shares, count=len(shares))
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{credential_id}/shares/{share_id}")
async def revoke_credential_share(
    credential_id: UUID,
    share_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> Message:
    """
    Revoke a credential share.
    """
    try:
        await CredentialShareService.revoke_credential_share(
            session=session,
            share_id=share_id,
            owner_id=current_user.id
        )
        return Message(message="Share revoked successfully")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/shared-with-me", response_model=SharedCredentialsPublic)
def get_credentials_shared_with_me(
    session: SessionDep,
    current_user: CurrentUser
) -> Any:
    """
    Get all credentials shared with the current user.
    These are credentials owned by others that you have read access to.
    """
    credentials = CredentialShareService.get_credentials_shared_with_me(
        session=session,
        user_id=current_user.id
    )
    return SharedCredentialsPublic(data=credentials, count=len(credentials))


@router.patch("/{credential_id}/sharing", response_model=CredentialPublic)
async def update_credential_sharing(
    credential_id: UUID,
    allow_sharing: bool = Body(..., embed=True),
    session: SessionDep = None,
    current_user: CurrentUser = None
) -> Any:
    """
    Enable or disable sharing for a credential.

    WARNING: Disabling sharing revokes ALL existing shares immediately, and
    unlinks the credential from every recipient agent.
    """
    try:
        credential = await CredentialShareService.update_credential_sharing(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
            allow_sharing=allow_sharing
        )
        return _credential_to_public(session, credential)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


# ── Group shares (user groups) ───────────────────────────────────────────────


@router.post(
    "/{credential_id}/group-shares", response_model=CredentialGroupSharePublic
)
def share_credential_with_group(
    credential_id: UUID,
    body: CredentialGroupShareCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Share a credential you own with a user group.

    Every current and future member can use it while they belong to the group.
    """
    try:
        return CredentialShareService.share_with_group(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
            group_id=body.group_id,
        )
    except CredentialGroupShareError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/{credential_id}/group-shares", response_model=CredentialGroupSharesPublic
)
def list_credential_group_shares(
    credential_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """The user groups a credential you own is shared with."""
    try:
        shares = CredentialShareService.list_group_shares(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
        )
    except CredentialGroupShareError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return CredentialGroupSharesPublic(data=shares, count=len(shares))


@router.delete("/{credential_id}/group-shares/{group_id}", response_model=Message)
async def revoke_credential_group_share(
    credential_id: UUID,
    group_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Stop sharing a credential with a user group.

    Members with no other path to it lose its agent links, and their running
    environments are re-synced without it.
    """
    try:
        await CredentialShareService.revoke_group_share(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
            group_id=group_id,
        )
    except CredentialGroupShareError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Message(message="Group share revoked successfully")
