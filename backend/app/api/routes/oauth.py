from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models import Message, OAuthConfig, Token
from app.services.users.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["oauth"])


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str


@router.get("/oauth/config")
def get_oauth_config() -> OAuthConfig:
    """Get OAuth provider availability and auth settings."""
    return OAuthConfig(
        google_enabled=AuthService.is_google_oauth_enabled(),
        allow_email_change=settings.allow_user_email_change,
    )


@router.get("/google/authorize")
def google_authorize() -> dict[str, str]:
    """Generate state token for Google OAuth flow."""
    if not AuthService.is_google_oauth_enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    state = AuthService.generate_oauth_state()
    auth_url = AuthService.build_google_authorization_url(state)

    return {"authorization_url": auth_url, "state": state}


@router.post("/google/callback")
async def google_callback(
    session: SessionDep, body: GoogleCallbackRequest
) -> Token:
    """Handle Google OAuth callback and issue JWT token."""
    if not AuthService.is_google_oauth_enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    try:
        _user, access_token = await AuthService.authenticate_with_google(
            session=session, code=body.code, state=body.state
        )
        return Token(access_token=access_token)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)}")


@router.post("/google/link", response_model=Message)
async def link_google_account_endpoint(
    session: SessionDep, current_user: CurrentUser, body: GoogleCallbackRequest
) -> Message:
    """Link Google account to current user."""
    try:
        await AuthService.link_google_account_for_user(
            session=session, user=current_user, code=body.code, state=body.state
        )
        return Message(message="Google account linked successfully")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to link account: {str(e)}")


@router.delete("/google/unlink", response_model=Message)
def unlink_google_account_endpoint(
    session: SessionDep, current_user: CurrentUser
) -> Message:
    """Unlink Google account from current user."""
    try:
        AuthService.unlink_google_account_for_user(session=session, user=current_user)
        return Message(message="Google account unlinked successfully")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
