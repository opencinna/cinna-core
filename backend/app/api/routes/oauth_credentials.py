import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select
from pydantic import BaseModel, Field, field_validator

from app.api.deps import CurrentUser, OptionalCurrentUser, SessionDep
from app.core.app_schemes import is_mobile_return_scheme
from app.models import Agent, Credential
from app.models.credentials.credential_oauth_state import CredentialOAuthOrigin
from app.services.bundles.install_readiness_gate import InstallReadinessGate
from app.services.credentials.oauth_credentials_service import OAuthCredentialsService
from app.services.credentials.credentials_service import CredentialsService


logger = logging.getLogger(__name__)

router = APIRouter()


# Request/Response models for OAuth endpoints
class OAuthAuthorizeRequest(BaseModel):
    # Cinna Mobile app scheme the callback page hands the browser back to
    # (``cinna-mobile`` or ``cinna-mobile-<suffix>``). Its presence marks a
    # mobile-started flow.
    return_scheme: str = Field(max_length=64)

    @field_validator("return_scheme")
    @classmethod
    def _validate_return_scheme(cls, v: str) -> str:
        if not is_mobile_return_scheme(v):
            raise ValueError("return_scheme must be a Cinna Mobile app scheme")
        return v


class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    # Expiry of the pending state, epoch seconds.
    expires_at: int | None = None


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


class OAuthCallbackResponse(BaseModel):
    credential_id: uuid.UUID
    message: str
    # Where the flow was started; the SPA picks its terminal view from it.
    origin: CredentialOAuthOrigin = "web"
    # Masked email of the Cinna account that owns the credential
    # ("ev•••@example.com"), so a user who completed someone else's
    # authorization link notices the account is not theirs.
    owner_email_masked: str | None = None


class OAuthMetadataResponse(BaseModel):
    user_email: str | None
    user_name: str | None
    scopes: list[str] | None
    expires_at: int | None
    granted_at: int | None
    refresh_error: str | None = None
    needs_reauthorization: bool = False


class OAuthRefreshResponse(BaseModel):
    message: str
    expires_at: int | None


@router.post("/{credential_id}/oauth/authorize", response_model=OAuthAuthorizeResponse)
def oauth_authorize(
    session: SessionDep,
    current_user: CurrentUser,
    credential_id: uuid.UUID,
    body: OAuthAuthorizeRequest | None = None,
) -> Any:
    """
    Initiate OAuth flow for a credential.

    Returns authorization URL and state token for OAuth flow.
    Only the credential owner can initiate OAuth. With a body carrying
    ``return_scheme`` (Cinna Mobile), the flow is mobile-started and the
    callback page returns the browser to that app scheme.
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
        if body is not None:
            result = OAuthCredentialsService.initiate_oauth_flow(
                session=session,
                credential_id=credential_id,
                user_id=current_user.id,
                origin="mobile",
                return_scheme=body.return_scheme,
            )
        else:
            result = OAuthCredentialsService.initiate_oauth_flow(
                session=session,
                credential_id=credential_id,
                user_id=current_user.id
            )
        return OAuthAuthorizeResponse(
            authorization_url=result["authorization_url"],
            state=result["state"],
            expires_at=result.get("expires_at"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise


@router.post("/oauth/callback", response_model=OAuthCallbackResponse)
async def oauth_callback(
    session: SessionDep,
    callback_data: OAuthCallbackRequest,
    signed_in_user: OptionalCurrentUser,
) -> Any:
    """
    Handle OAuth callback from Google.

    Exchanges authorization code for access/refresh tokens and stores them.
    Public: a desktop-started flow completes in a browser with no Cinna
    session. When the browser is signed in, the state must belong to that
    account (403 otherwise, state consumed). The response names the owning
    account (masked email) so the page can show it.
    """
    try:
        # Handle OAuth callback using service
        result = await OAuthCredentialsService.handle_oauth_callback(
            session=session,
            code=callback_data.code,
            state=callback_data.state,
            signed_in_user_id=signed_in_user.id if signed_in_user else None,
        )
        credential = result.credential

        # Trigger credential sync to affected agent environments
        await CredentialsService.event_credential_updated(
            session=session,
            credential_id=credential.id
        )

        if result.placeholder_cleared:
            await _emit_setup_completed_for_installs(session, credential)

        return OAuthCallbackResponse(
            credential_id=credential.id,
            message="OAuth authorization successful",
            origin=result.origin,
            owner_email_masked=result.owner_email_masked,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {str(e)}")


async def _emit_setup_completed_for_installs(
    session: SessionDep, credential: Credential
) -> None:
    """Notify each of the owner's bundle installs linked to ``credential`` that is now ready.

    Mirrors the setup-credential fill path. The gate helper swallows its own
    errors, so a stored grant never turns into a failed callback.
    """
    try:
        agent_ids = CredentialsService.get_affected_agents(session, credential.id)
        for agent_id in agent_ids:
            agent = session.get(Agent, agent_id)
            # Only bundle installs have a setup banner; plain agents are skipped.
            if (
                agent is None
                or agent.owner_id != credential.owner_id
                or agent.bundle_uuid is None
            ):
                continue
            await InstallReadinessGate.emit_setup_completed_if_ready(
                session, agent, credential.id
            )
    except Exception as e:  # pragma: no cover — diagnostic-only path
        logger.warning(
            "Failed to emit setup-completed after OAuth for credential %s: %s",
            credential.id, e,
        )


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
        granted_at=metadata.get("granted_at"),
        refresh_error=metadata.get("refresh_error"),
        needs_reauthorization=bool(metadata.get("needs_reauthorization", False)),
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
