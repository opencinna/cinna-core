from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models import LoginResponse, LoginToken, Message, MfaChallenge, OAuthConfig
from app.services.users.auth_service import AuthService
from app.services.users.mfa_service import MfaService

router = APIRouter(prefix="/auth", tags=["oauth"])


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str
    # Opaque trusted-device token ("Do not ask on this device"). When a
    # valid unexpired token is presented for a 2FA user, the callback
    # returns a ``LoginToken`` directly and skips the challenge. Absent or
    # forged → normal challenge path. Only read on the login callback, not
    # on ``/google/link``.
    trusted_device_token: str | None = None


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


@router.post("/google/callback", response_model=LoginResponse)
async def google_callback(
    session: SessionDep, body: GoogleCallbackRequest
):
    """Handle Google OAuth callback.

    Returns the same discriminated :class:`LoginResponse` as
    ``POST /login/access-token``:

    - :class:`LoginToken` (``kind="token"``) for users without 2FA.
    - :class:`MfaChallenge` (``kind="mfa_challenge"``) when the user has
      2FA enabled — the frontend completes via ``/login/mfa/verify``.
    """
    if not AuthService.is_google_oauth_enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    try:
        result = await AuthService.authenticate_with_google(
            session=session, code=body.code, state=body.state
        )

        if result.requires_mfa:
            # "Do not ask on this device" skip — only attempted when 2FA
            # is on (Google identity already verified above). The result's
            # ``access_token`` is None on the MFA branch, so mint it here
            # via the same helper the non-MFA branch uses.
            if MfaService.consume_trusted_device(
                session=session,
                user=result.user,
                token=body.trusted_device_token,
            ):
                return LoginToken(
                    access_token=AuthService.create_access_token(result.user.id)
                )
            challenge = result.mfa_challenge
            assert challenge is not None  # for mypy; guarded by requires_mfa
            return MfaChallenge(
                challenge_token=challenge.challenge_token,
                expires_at=challenge.expires_at,
                allowed_methods=MfaService.allowed_methods_for_user(
                    session=session, user=result.user
                ),
            )

        assert result.access_token is not None
        return LoginToken(access_token=result.access_token)

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
