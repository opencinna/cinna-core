import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app import crud
from app.api.deps import CurrentUser, SessionDep
from app.core import security
from app.core.config import settings
from app.models import Message, OAuthConfig, Token

router = APIRouter(prefix="/auth", tags=["oauth"])

# In-memory state storage (use Redis in production)
_oauth_states: dict[str, dict[str, Any]] = {}


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str


@router.get("/oauth/config")
def get_oauth_config() -> OAuthConfig:
    """Get OAuth provider availability."""
    return OAuthConfig(google_enabled=settings.google_oauth_enabled)


@router.get("/google/authorize")
def google_authorize() -> dict[str, str]:
    """Generate state token for Google OAuth flow."""
    if not settings.google_oauth_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    # Generate CSRF state token
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "expires": datetime.now(timezone.utc).timestamp() + 600  # 10 minutes
    }

    # Clean up expired states
    now = datetime.now(timezone.utc).timestamp()
    expired_states = [k for k, v in _oauth_states.items() if v["expires"] < now]
    for k in expired_states:
        del _oauth_states[k]

    # Build authorization URL
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={settings.GOOGLE_CLIENT_ID}&"
        f"redirect_uri={settings.GOOGLE_REDIRECT_URI}&"
        "response_type=code&"
        "scope=openid%20email%20profile&"
        f"state={state}"
    )

    return {"authorization_url": auth_url, "state": state}


@router.post("/google/callback")
async def google_callback(
    session: SessionDep, body: GoogleCallbackRequest
) -> Token:
    """Handle Google OAuth callback and issue JWT token."""
    if not settings.google_oauth_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    # Note: State validation skipped for popup flow (@react-oauth/google)
    # The popup flow has built-in CSRF protection via browser same-origin policy
    # Clean up state if it exists (for backwards compatibility)
    _oauth_states.pop(body.state, None)

    try:
        # Exchange code for token
        import httpx

        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": body.code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": "postmessage",  # Required for popup flow with @react-oauth/google
                    "grant_type": "authorization_code",
                },
            )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400, detail="Failed to exchange authorization code"
            )

        token_data = token_response.json()
        id_token = token_data.get("id_token")

        if not id_token:
            raise HTTPException(status_code=400, detail="No ID token received")

        # Verify and decode ID token
        claims = await security.verify_google_token(id_token, settings.GOOGLE_CLIENT_ID)  # type: ignore
        if not claims:
            raise HTTPException(status_code=400, detail="Invalid Google token")

        google_id = claims["sub"]
        email = claims["email"]
        full_name = claims.get("name")

        # Check if user exists by Google ID
        user = crud.get_user_by_google_id(session=session, google_id=google_id)

        if not user:
            # Check if user exists by email (auto-link)
            user = crud.get_user_by_email(session=session, email=email)
            if user:
                # Auto-link Google account to existing email user
                user = crud.link_google_account(
                    session=session, user=user, google_id=google_id
                )
            else:
                # Create new user
                user = crud.create_user_from_google(
                    session=session,
                    email=email,
                    google_id=google_id,
                    full_name=full_name,
                )

        if not user.is_active:
            raise HTTPException(status_code=400, detail="Inactive user")

        # Generate JWT access token
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = security.create_access_token(
            user.id, expires_delta=access_token_expires
        )

        return Token(access_token=access_token)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)}")


@router.post("/google/link", response_model=Message)
async def link_google_account_endpoint(
    session: SessionDep, current_user: CurrentUser, body: GoogleCallbackRequest
) -> Message:
    """Link Google account to current user."""
    if current_user.google_id:
        raise HTTPException(status_code=400, detail="Google account already linked")

    # Note: State validation skipped for popup flow (@react-oauth/google)
    _oauth_states.pop(body.state, None)

    try:
        # Exchange code for token
        import httpx

        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": body.code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": "postmessage",  # Required for popup flow with @react-oauth/google
                    "grant_type": "authorization_code",
                },
            )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400, detail="Failed to exchange authorization code"
            )

        token_data = token_response.json()
        claims = await security.verify_google_token(
            token_data["id_token"], settings.GOOGLE_CLIENT_ID  # type: ignore
        )

        if not claims:
            raise HTTPException(status_code=400, detail="Invalid Google token")

        google_id = claims["sub"]

        # Check if Google ID is already used
        existing_user = crud.get_user_by_google_id(session=session, google_id=google_id)
        if existing_user:
            raise HTTPException(
                status_code=400,
                detail="This Google account is already linked to another user",
            )

        # Link account
        crud.link_google_account(
            session=session, user=current_user, google_id=google_id
        )
        return Message(message="Google account linked successfully")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to link account: {str(e)}")


@router.delete("/google/unlink", response_model=Message)
def unlink_google_account_endpoint(
    session: SessionDep, current_user: CurrentUser
) -> Message:
    """Unlink Google account from current user."""
    if not current_user.google_id:
        raise HTTPException(status_code=400, detail="No Google account linked")

    # Prevent unlinking if no password set (would lock out user)
    if not current_user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="Cannot unlink Google account without setting a password first",
        )

    crud.unlink_google_account(session=session, user=current_user)
    return Message(message="Google account unlinked successfully")
