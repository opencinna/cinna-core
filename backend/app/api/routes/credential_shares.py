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
from app.services.credential_share_service import CredentialShareService

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
        owner_id=credential.owner_id,
        user_workspace_id=credential.user_workspace_id,
        share_count=share_count
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
            shared_with_email=share_data.shared_with_email
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
def revoke_credential_share(
    credential_id: UUID,
    share_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> Message:
    """
    Revoke a credential share.
    """
    try:
        CredentialShareService.revoke_credential_share(
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
def update_credential_sharing(
    credential_id: UUID,
    allow_sharing: bool = Body(..., embed=True),
    session: SessionDep = None,
    current_user: CurrentUser = None
) -> Any:
    """
    Enable or disable sharing for a credential.

    WARNING: Disabling sharing revokes ALL existing shares immediately.
    """
    try:
        credential = CredentialShareService.update_credential_sharing(
            session=session,
            credential_id=credential_id,
            owner_id=current_user.id,
            allow_sharing=allow_sharing
        )
        return _credential_to_public(session, credential)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))
