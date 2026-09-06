from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.models import LoginResponse, LoginToken, Message, MfaChallenge, OAuthConfig
from app.services.users.access_policy_service import RegistrationNotAllowedError
from app.services.users.auth_service import AuthService
from app.services.users.invitation_service import InvitationService
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
    return OAuthConfig(google_enabled=AuthService.is_google_oauth_enabled())


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

    A Google login on a pre-created invited account also settles that
    invitation. The hook is deliberately below the error handlers rather than
    inside the ``try`` — see the comment there.
    """
    if not AuthService.is_google_oauth_enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured",
        )

    response: LoginToken | MfaChallenge
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
                response = LoginToken(
                    access_token=AuthService.create_access_token(result.user.id)
                )
            else:
                challenge = result.mfa_challenge
                assert challenge is not None  # for mypy; guarded by requires_mfa
                response = MfaChallenge(
                    challenge_token=challenge.challenge_token,
                    expires_at=challenge.expires_at,
                    allowed_methods=MfaService.allowed_methods_for_user(
                        session=session, user=result.user
                    ),
                )
        else:
            assert result.access_token is not None
            response = LoginToken(access_token=result.access_token)
        authenticated_user = result.user

    except RegistrationNotAllowedError as e:
        # Same status and body as the signup route: one refusal shape for
        # every registration path, and no hint about whether the address
        # already has an account.
        raise HTTPException(status_code=403, detail=e.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)}")

    # ── Outside every net above, and that placement is the whole point ──
    #
    # The blanket ``except Exception`` a few lines up turns anything raised
    # inside the ``try`` into ``400 "OAuth error"``. A failure to tick an
    # invitation as accepted would therefore become a **failed login for an
    # account that just successfully authenticated with Google**. Marking an
    # invitation accepted is a side record; if it fails, the correct outcome
    # is a signed-in person and a stale ``pending`` row the admin can see and
    # resend. ``mark_accepted_if_pending`` is also non-raising by contract and
    # leaves the session usable — belt and braces, because the cost of getting
    # this wrong is someone locked out of their own new account.
    #
    # Only on the token arm: an MFA challenge means the person has *not*
    # finished authenticating yet, and settling their invitation there would
    # record an acceptance that never happened.
    if isinstance(response, LoginToken):
        await InvitationService.mark_accepted_if_pending(
            session, authenticated_user, method="google"
        )
    return response


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

    except RegistrationNotAllowedError as e:
        # Listed above the ``ValueError`` arm, which would otherwise swallow it
        # — ``RegistrationNotAllowedError`` is a ``ValueError`` subclass, and
        # the first matching arm wins. Same status and body the callback gives
        # the same refusal, so the two routes cannot disagree about what a
        # policy refusal looks like.
        #
        # Not reachable today: ``link_google_account_for_user`` refuses anyone
        # who already has a ``google_id``, so everyone who reaches the claim
        # gate inside ``link_google_account`` authenticated with a password and
        # is therefore claimed. Kept because the *only* thing standing between
        # here and a mislabelled 400 is that reachability argument, and a
        # handler is cheaper than re-deriving it after the next change.
        raise HTTPException(status_code=403, detail=e.reason)
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
