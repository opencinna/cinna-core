"""Desktop App Authentication API routes.

Implements the server-side OAuth 2.0 + PKCE flow for Cinna Desktop clients,
using a consent-page pattern that works behind nginx proxying only /api, /mcp,
and /.well-known/*.

Endpoints:
  GET    /desktop-auth/clients             List user's desktop clients
  DELETE /desktop-auth/clients/{client_id} Revoke a desktop client
  GET    /desktop-auth/authorize           OAuth authorization (public — redirects to consent page)
  GET    /desktop-auth/requests/{nonce}    Consent page metadata (public)
  POST   /desktop-auth/consent            Process user consent (requires login)
  POST   /desktop-auth/token              Token exchange / refresh (public)
  GET    /desktop-auth/userinfo           Current user profile for the bearer token
  POST   /desktop-auth/revoke             Revoke a client or token (requires login)

The /.well-known/cinna-desktop discovery endpoint is registered at the app
level in main.py (not under /api/v1).
"""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ValidationError

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models.desktop_auth.desktop_oauth_client import (
    DesktopOAuthClientPublic,
)
from app.services.desktop_auth.desktop_auth_service import DesktopAuthService

router = APIRouter(prefix="/desktop-auth", tags=["desktop-auth"])


# ── Client management ──────────────────────────────────────────────────────


@router.get("/clients", response_model=list[DesktopOAuthClientPublic])
def list_desktop_clients(
    session: SessionDep,
    current_user: CurrentUser,
) -> list[DesktopOAuthClientPublic]:
    """List all active desktop clients registered by the authenticated user."""
    return DesktopAuthService.list_clients(session, current_user.id)


@router.delete("/clients/{client_id}", status_code=204)
def revoke_desktop_client(
    client_id: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """Revoke a desktop client and all its refresh tokens."""
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

    Does NOT require authentication. The browser navigates here directly, so the
    JWT in localStorage cannot be included. Instead, we store the request by nonce
    and redirect to the SPA consent page, which uses its stored JWT to call
    POST /consent.

    Either client_id (existing client) or device_name (lazy registration) must
    be provided so the consent page can display meaningful information.
    """
    if code_challenge_method != "S256":
        raise HTTPException(
            status_code=400, detail="unsupported_code_challenge_method"
        )

    # Validate redirect_uri before storing anything
    from app.services.desktop_auth.desktop_auth_service import _validate_redirect_uri
    _validate_redirect_uri(redirect_uri)

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
        url=f"{frontend_host}/desktop-auth/consent?request={nonce}",
        status_code=307,
    )


# ── Consent request metadata ───────────────────────────────────────────────


@router.get("/requests/{nonce}")
def get_auth_request(
    nonce: str,
    session: SessionDep,
) -> dict:
    """Return non-secret display metadata for a pending consent request.

    Public endpoint — no authentication required. Used by the frontend consent
    page to render the device name, platform, and app version before the user
    approves or denies.

    Returns 404 if the nonce is unknown, already used, or expired.
    """
    data = DesktopAuthService.get_auth_request(session, nonce)
    if data is None:
        raise HTTPException(status_code=404, detail="not_found")
    return data


# ── Consent processing ─────────────────────────────────────────────────────


class ConsentRequest(BaseModel):
    request_nonce: str
    action: str  # "approve" or "deny"


class ConsentResponse(BaseModel):
    redirect_to: str


@router.post("/consent", response_model=ConsentResponse)
def consent(
    body: ConsentRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> ConsentResponse:
    """Process user consent for a desktop auth request.

    Requires authentication (the SPA calls this with its localStorage JWT).

    action="approve":
      - Resolves or lazily creates the desktop client
      - Issues a single-use authorization code
      - Returns redirect URL with the code for the desktop app to capture
    action="deny":
      - Marks the request as used
      - Returns redirect URL with error=access_denied
    """
    if body.action not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="invalid_action")

    result = DesktopAuthService.process_consent(
        session, current_user.id, body.request_nonce, body.action
    )
    return ConsentResponse(redirect_to=result["redirect_to"])


# ── Token endpoint ─────────────────────────────────────────────────────────


class TokenRequest(BaseModel):
    grant_type: str
    client_id: str
    # authorization_code fields
    code: str | None = None
    redirect_uri: str | None = None
    code_verifier: str | None = None
    # refresh_token fields
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    client_id: str


async def _parse_token_request(request: Request) -> TokenRequest:
    """Parse the /token body from either JSON or application/x-www-form-urlencoded.

    OAuth 2.0 (RFC 6749 §3.2) requires the token endpoint to accept
    application/x-www-form-urlencoded. We also keep JSON support so existing
    tests and any JSON-based clients continue to work.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded" or content_type == "multipart/form-data":
        form = await request.form()
        payload = {k: v for k, v in form.items() if isinstance(v, str)}
    else:
        # Default to JSON (also covers clients that omit content-type)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_request_body")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid_request_body")

    try:
        return TokenRequest(**payload)
    except ValidationError:
        raise HTTPException(status_code=400, detail="invalid_request_body")


@router.post("/token", response_model=TokenResponse)
async def token_endpoint(
    request: Request,
    session: SessionDep,
) -> TokenResponse:
    """Token endpoint — exchange authorization code or rotate refresh token.

    Accepts both ``application/x-www-form-urlencoded`` (OAuth 2.0 RFC 6749
    standard) and ``application/json`` request bodies. The endpoint is public
    (no authentication required). The response includes client_id so desktop
    apps using lazy registration learn their assigned client_id after the
    first code exchange.
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


class UserInfoResponse(BaseModel):
    sub: str
    email: str
    full_name: str | None = None
    username: str | None = None


@router.get("/userinfo", response_model=UserInfoResponse)
def userinfo(current_user: CurrentUser) -> UserInfoResponse:
    """Return basic profile info for the authenticated user.

    Intended for Cinna Desktop clients to display "Connected as {name}
    ({email})" after a successful token exchange. The desktop access token
    is a standard JWT, so this endpoint works with the same CurrentUser
    dependency as the rest of the API.
    """
    return UserInfoResponse(
        sub=str(current_user.id),
        email=current_user.email,
        full_name=current_user.full_name,
        username=current_user.username,
    )


# ── Revocation endpoint ────────────────────────────────────────────────────


class RevokeRequest(BaseModel):
    client_id: str | None = None
    refresh_token: str | None = None


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
