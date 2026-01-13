import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.models import Credential
from app.services.oauth_credentials_service import OAuthCredentialsService
from app.services.credentials_service import CredentialsService


router = APIRouter()


# Request/Response models for OAuth endpoints
class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


class OAuthCallbackResponse(BaseModel):
    credential_id: uuid.UUID
    message: str


class OAuthMetadataResponse(BaseModel):
    user_email: str | None
    user_name: str | None
    scopes: list[str] | None
    expires_at: int | None
    granted_at: int | None


class OAuthRefreshResponse(BaseModel):
    message: str
    expires_at: int | None


@router.post("/{credential_id}/oauth/authorize", response_model=OAuthAuthorizeResponse)
def oauth_authorize(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
) -> Any:
    """
    Initiate OAuth flow for a credential.

    Returns authorization URL and state token for OAuth flow.
    Only the credential owner can initiate OAuth.
    """
    # Verify credential exists and user owns it
    # Credentials are always private - only owner can access
    credential = session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if credential.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        # Initiate OAuth flow using service
        result = OAuthCredentialsService.initiate_oauth_flow(
            session=session,
            credential_id=credential_id,
            user_id=current_user.id
        )
        return OAuthAuthorizeResponse(
            authorization_url=result["authorization_url"],
            state=result["state"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise


@router.post("/oauth/callback", response_model=OAuthCallbackResponse)
async def oauth_callback(
    session: SessionDep,
    callback_data: OAuthCallbackRequest,
) -> Any:
    """
    Handle OAuth callback from Google.

    Exchanges authorization code for access/refresh tokens and stores them.
    This is a public endpoint (no auth required) as it's called by Google redirect.
    """
    try:
        # Handle OAuth callback using service
        credential = await OAuthCredentialsService.handle_oauth_callback(
            session=session,
            code=callback_data.code,
            state=callback_data.state
        )

        # Trigger credential sync to affected agent environments
        await CredentialsService.event_credential_updated(
            session=session,
            credential_id=credential.id
        )

        return OAuthCallbackResponse(
            credential_id=credential.id,
            message="OAuth authorization successful"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {str(e)}")


@router.get("/{credential_id}/oauth/metadata", response_model=OAuthMetadataResponse)
def get_oauth_metadata(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
) -> Any:
    """
    Get OAuth metadata for a credential.

    Returns non-sensitive OAuth information like user email, scopes, expiration.
    Only the credential owner can view metadata.
    """
    # Verify credential exists and user owns it
    # Credentials are always private - only owner can access
    credential = session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if credential.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Extract OAuth metadata using service
    metadata = OAuthCredentialsService.get_oauth_metadata(
        session=session,
        credential=credential
    )

    return OAuthMetadataResponse(
        user_email=metadata.get("user_email"),
        user_name=metadata.get("user_name"),
        scopes=metadata.get("scopes"),
        expires_at=metadata.get("expires_at"),
        granted_at=metadata.get("granted_at")
    )


@router.post("/{credential_id}/oauth/refresh", response_model=OAuthRefreshResponse)
async def refresh_oauth_token(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
) -> Any:
    """
    Manually refresh OAuth token for a credential.

    Uses refresh token to obtain new access token.
    Only the credential owner can refresh tokens.
    """
    # Verify credential exists and user owns it
    # Credentials are always private - only owner can access
    credential = session.get(Credential, credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if credential.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        # Refresh OAuth token using service
        updated_credential = await OAuthCredentialsService.refresh_oauth_token(
            session=session,
            credential=credential
        )

        # Trigger credential sync to affected agent environments
        await CredentialsService.event_credential_updated(
            session=session,
            credential_id=updated_credential.id
        )

        # Get updated expiration time
        metadata = OAuthCredentialsService.get_oauth_metadata(
            session=session,
            credential=updated_credential
        )

        return OAuthRefreshResponse(
            message="Token refresh successful",
            expires_at=metadata.get("expires_at")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token refresh failed: {str(e)}")
