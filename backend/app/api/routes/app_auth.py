"""App Authentication API routes (native mobile clients).

A parallel OAuth 2.0 + PKCE surface for Cinna Mobile, mounted at ``/app-auth``.
It shares the *same* backing service, storage tables, and token logic as the
desktop flow (``DesktopAuthService``) — the only differences are the URL
namespace and the consent-page redirect target. Desktop clients keep using
``/desktop-auth`` unchanged.

Both surfaces write to the same ``desktop_*`` tables, so a consent request
created here is visible to either consent endpoint and vice-versa. The
``client_kind`` derived from the redirect_uri (``mobile`` for ``cinna-mobile://``
/ ``exp://``, ``desktop`` for loopback) drives the consent-screen copy.

Endpoints (mirror of ``/desktop-auth``):
  GET    /app-auth/clients             List user's clients
  DELETE /app-auth/clients/{client_id} Revoke a client
  GET    /app-auth/authorize           OAuth authorization (public — redirects to consent page)
  GET    /app-auth/requests/{nonce}    Consent page metadata (public)
  POST   /app-auth/consent            Process user consent (requires login)
  POST   /app-auth/token              Token exchange / refresh (public)
  GET    /app-auth/userinfo           Current user profile for the bearer token
  POST   /app-auth/revoke             Revoke a client or token (requires login)

The /.well-known/cinna-app discovery endpoint is registered at the app level in
main.py (not under /api/v1).
"""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.desktop_auth import (
    ConsentRequest,
    ConsentResponse,
    RevokeRequest,
    TokenResponse,
    UserInfoResponse,
    _parse_token_request,
)
from app.core.config import settings
from app.models.desktop_auth.desktop_oauth_client import (
    DesktopOAuthClientPublic,
)
from app.services.desktop_auth.desktop_auth_service import (
    DesktopAuthService,
)

router = APIRouter(prefix="/app-auth", tags=["app-auth"])


# ── Client management ──────────────────────────────────────────────────────


@router.get("/clients", response_model=list[DesktopOAuthClientPublic])
def list_app_clients(
    session: SessionDep,
    current_user: CurrentUser,
) -> list[DesktopOAuthClientPublic]:
    """List all active app clients registered by the authenticated user."""
    return DesktopAuthService.list_clients(session, current_user.id)


@router.delete("/clients/{client_id}", status_code=204)
def revoke_app_client(
    client_id: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """Revoke an app client and all its refresh tokens."""
    DesktopAuthService.revoke_client(session, current_user.id, client_id)


# ── OAuth authorization code flow ─────────────────────────────────────────


@router.get("/authorize")
def authorize(
    session: SessionDep,
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    state: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    client_id: str | None = Query(default=None),
    device_name: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    app_version: str | None = Query(default=None),
) -> RedirectResponse:
    """Public authorization endpoint — stores consent request and redirects to frontend.

    Identical to the desktop flow except the browser is redirected to the
    ``/app-auth/consent`` SPA page. Does NOT require authentication; the consent
    page uses its stored JWT to call ``POST /app-auth/consent``.

    Either client_id (existing client) or device_name (lazy registration) must
    be provided so the consent page can display meaningful information.
    """
    if code_challenge_method != "S256":
        raise HTTPException(
            status_code=400, detail="unsupported_code_challenge_method"
        )

    # Validate redirect_uri before storing anything
    DesktopAuthService.validate_redirect_uri(redirect_uri)

    if not client_id and not device_name:
        raise HTTPException(
            status_code=400, detail="must provide client_id or device_name"
        )

    # If client_id is provided, verify it exists and is not revoked
    if client_id:
        from sqlmodel import select
        from app.models.desktop_auth.desktop_oauth_client import DesktopOAuthClient
        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.client_id == client_id,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        client = session.exec(stmt).first()
        if not client:
            raise HTTPException(status_code=400, detail="invalid_client")

    # Store the pending consent request and get the nonce back
    nonce = DesktopAuthService.create_auth_request(
        session,
        device_name=device_name,
        platform=platform,
        app_version=app_version,
        client_id=client_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        state=state,
    )

    frontend_host = settings.FRONTEND_HOST.rstrip("/")
    return RedirectResponse(
        url=f"{frontend_host}/app-auth/consent?request={nonce}",
        status_code=307,
    )


# ── Consent request metadata ───────────────────────────────────────────────


@router.get("/requests/{nonce}")
def get_app_auth_request(
    nonce: str,
    session: SessionDep,
) -> dict:
    """Return non-secret display metadata for a pending consent request.

    Public endpoint — no authentication required. Used by the frontend consent
    page to render the device name, platform, app version, and client_kind
    before the user approves or denies.

    Returns 404 if the nonce is unknown, already used, or expired.
    """
    data = DesktopAuthService.get_auth_request(session, nonce)
    if data is None:
        raise HTTPException(status_code=404, detail="not_found")
    return data


# ── Consent processing ─────────────────────────────────────────────────────


@router.post("/consent", response_model=ConsentResponse)
def app_consent(
    body: ConsentRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> ConsentResponse:
    """Process user consent for an app auth request.

    Requires authentication (the SPA calls this with its localStorage JWT).
    Behaviour matches the desktop flow — see ``DesktopAuthService.process_consent``.
    """
    if body.action not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="invalid_action")

    result = DesktopAuthService.process_consent(
        session, current_user.id, body.request_nonce, body.action
    )
    return ConsentResponse(redirect_to=result["redirect_to"])


# ── Token endpoint ─────────────────────────────────────────────────────────


@router.post("/token", response_model=TokenResponse)
async def token_endpoint(
    request: Request,
    session: SessionDep,
) -> TokenResponse:
    """Token endpoint — exchange authorization code or rotate refresh token.

    Accepts both ``application/x-www-form-urlencoded`` (OAuth 2.0 RFC 6749
    standard) and ``application/json`` request bodies. Public (no auth). The
    response includes client_id so lazy-registered clients learn their assigned
    client_id after the first code exchange.
    """
    body = await _parse_token_request(request)
    if body.grant_type == "authorization_code":
        if not body.code or not body.redirect_uri or not body.code_verifier:
            raise HTTPException(status_code=400, detail="missing_parameters")
        result = DesktopAuthService.exchange_code(
            session,
            body.code,
            body.client_id,
            body.redirect_uri,
            body.code_verifier,
        )
    elif body.grant_type == "refresh_token":
        if not body.refresh_token:
            raise HTTPException(status_code=400, detail="missing_parameters")
        result = DesktopAuthService.refresh_tokens(
            session,
            body.refresh_token,
            body.client_id,
        )
    else:
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    return TokenResponse(**result)


# ── User info endpoint ─────────────────────────────────────────────────────


@router.get("/userinfo", response_model=UserInfoResponse)
def userinfo(current_user: CurrentUser) -> UserInfoResponse:
    """Return basic profile info for the authenticated user.

    Intended for Cinna Mobile clients to display "Connected as {name} ({email})"
    after a successful token exchange. The access token is a standard JWT, so
    this works with the same CurrentUser dependency as the rest of the API.
    """
    return UserInfoResponse(
        sub=str(current_user.id),
        email=current_user.email,
        full_name=current_user.full_name,
        username=current_user.username,
    )


# ── Revocation endpoint ────────────────────────────────────────────────────


@router.post("/revoke", status_code=204)
def revoke(
    body: RevokeRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """Revoke a client (and all its tokens) or a specific refresh token."""
    if body.client_id:
        DesktopAuthService.revoke_client(session, current_user.id, body.client_id)
    elif body.refresh_token:
        DesktopAuthService.revoke_by_refresh_token(
            session, current_user.id, body.refresh_token
        )
    else:
        raise HTTPException(
            status_code=400, detail="must provide client_id or refresh_token"
        )
