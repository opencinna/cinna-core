"""Account-scoped, audited credential delivery to authenticated Desktop devices.

Business rules live in ``DesktopCredentialService``; this module only maps
HTTP (auth claims, ETag/cache headers, errors) onto it.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.api.deps import (
    CurrentClientClaims,
    CurrentUser,
    SessionDep,
    ensure_not_cli_exchanged_session,
)
from app.models.credentials.credential import (
    AgentCredentialAccessTokenRequest,
    AgentCredentialAccessTokenResponse,
)
from app.models.credentials.desktop_credential import (
    DesktopCredentialList,
    DesktopCredentialMaterializeRequest,
    DesktopCredentialMaterializeResponse,
    DesktopOAuthAuthorizeResponse,
    DesktopOAuthCredentialCreate,
)
from app.services.credentials.desktop_credential_service import (
    DesktopCredentialError,
    DesktopCredentialService,
)
from app.services.credentials.desktop_oauth_service import DesktopOAuthService

router = APIRouter(prefix="/external/credentials", tags=["external"])

INCLUDE_AGENT_API_KEYS = "agent_api_keys"


def _desktop_client_id(session: SessionDep, client_claims: CurrentClientClaims) -> str:
    """The calling Desktop client's id; 403 for non-Desktop claims and for a
    desktop JWT bought with an account CLI token (no secret export)."""
    kind, client_id = client_claims
    if kind != "desktop" or not client_id:
        raise HTTPException(403, "Desktop authentication required")
    ensure_not_cli_exchanged_session(session, client_id)
    return client_id


def _http_error(e: DesktopCredentialError) -> HTTPException:
    """Coded errors answer ``detail={"code", "message"}``; uncoded ones keep
    the plain string detail."""
    detail = {"code": e.code, "message": e.message} if e.code else e.message
    return HTTPException(e.status_code, detail, headers=e.headers)


def _includes_agent_api_keys(include: str | None) -> bool:
    """``include`` is comma-separated; unknown values are ignored."""
    if not include:
        return False
    return INCLUDE_AGENT_API_KEYS in {part.strip() for part in include.split(",")}


@router.get("", response_model=DesktopCredentialList)
def list_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    request: Request,
    response: Response,
    include: str | None = Query(None),
) -> DesktopCredentialList | Response:
    """Metadata of credentials the caller may attach to local agents. No secrets.

    ``include=agent_api_keys`` opts in to the caller's own issued agent_api
    keys (listed whatever their ``key_status``).
    """
    etag, result = DesktopCredentialService.list_credentials(
        session,
        current_user,
        request.headers.get("if-none-match"),
        include_agent_api_keys=_includes_agent_api_keys(include),
    )
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if result is None:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return result


@router.post("/materialize", response_model=DesktopCredentialMaterializeResponse)
async def materialize_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    body: DesktopCredentialMaterializeRequest,
    response: Response,
    include: str | None = Query(None),
) -> DesktopCredentialMaterializeResponse:
    """Deliver the selected credentials' values to an interactive Desktop client.

    Without ``include=agent_api_keys`` any agent_api id is refused
    ``unsupported_type``; with it, the caller's own active issued keys are
    delivered and non-active ones refused ``key_revoked`` / ``key_expired`` /
    ``key_paused`` with ``key_status``.
    """
    client_id = _desktop_client_id(session, client_claims)
    try:
        result = await DesktopCredentialService.materialize(
            session,
            current_user,
            client_id,
            body,
            include_agent_api_keys=_includes_agent_api_keys(include),
        )
    except DesktopCredentialError as e:
        raise _http_error(e) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/oauth",
    response_model=DesktopOAuthAuthorizeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_oauth_credential(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    body: DesktopOAuthCredentialCreate,
) -> DesktopOAuthAuthorizeResponse:
    """Create a Google OAuth credential (local use allowed, Default workspace)
    and return the URL that authorizes it.

    Errors carry ``detail = {"code", "message"}``: 422 ``unsupported_type``,
    503 ``oauth_not_configured`` (both before anything is created).
    """
    client_id = _desktop_client_id(session, client_claims)
    try:
        return await DesktopOAuthService.create_oauth_credential(
            session, current_user, client_id, body
        )
    except DesktopCredentialError as e:
        raise _http_error(e) from None


@router.post(
    "/{credential_id}/oauth/authorize",
    response_model=DesktopOAuthAuthorizeResponse,
)
async def reauthorize_oauth_credential(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    credential_id: uuid.UUID,
) -> DesktopOAuthAuthorizeResponse:
    """Start a new Google authorization for a credential the caller owns.

    Errors carry ``detail = {"code", "message"}``: 404 ``credential_not_found``
    (missing or not owned), 422 ``unsupported_type``, 503 ``oauth_not_configured``.
    """
    client_id = _desktop_client_id(session, client_claims)
    try:
        return await DesktopOAuthService.reauthorize_oauth_credential(
            session, current_user, client_id, credential_id
        )
    except DesktopCredentialError as e:
        raise _http_error(e) from None


@router.post(
    "/{credential_id}/access-token",
    response_model=AgentCredentialAccessTokenResponse,
)
async def desktop_credential_access_token(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    credential_id: uuid.UUID,
    body: AgentCredentialAccessTokenRequest,
    response: Response,
) -> AgentCredentialAccessTokenResponse:
    """A Google access token valid for at least ``min_ttl`` seconds, for a
    credential the caller may use locally. The refresh token never leaves Core.

    Errors carry ``detail = {"code", "message"}``: 404 ``credential_not_found``,
    422 ``not_refreshable``, 409 ``reauthorization_required``,
    502 ``provider_error`` / ``refresh_in_progress`` (with ``Retry-After``),
    503 ``oauth_not_configured``.
    """
    client_id = _desktop_client_id(session, client_claims)
    try:
        result = await DesktopOAuthService.access_token(
            session, current_user, client_id, credential_id, body
        )
    except DesktopCredentialError as e:
        raise _http_error(e) from None
    response.headers["Cache-Control"] = "no-store"
    return result
