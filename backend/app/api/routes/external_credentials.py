"""Account-scoped, audited credential delivery to authenticated Desktop devices.

Business rules live in ``DesktopCredentialService``; this module only maps
HTTP (auth claims, ETag/cache headers, errors) onto it.
"""

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.deps import (
    CurrentClientClaims,
    CurrentUser,
    SessionDep,
    ensure_not_cli_exchanged_session,
)
from app.models.credentials.desktop_credential import (
    DesktopCredentialList,
    DesktopCredentialMaterializeRequest,
    DesktopCredentialMaterializeResponse,
)
from app.services.credentials.desktop_credential_service import (
    DesktopCredentialError,
    DesktopCredentialService,
)

router = APIRouter(prefix="/external/credentials", tags=["external"])


@router.get("", response_model=DesktopCredentialList)
def list_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    request: Request,
    response: Response,
) -> DesktopCredentialList | Response:
    """Metadata of credentials the caller may attach to local agents. No secrets."""
    etag, result = DesktopCredentialService.list_credentials(
        session, current_user, request.headers.get("if-none-match")
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
) -> DesktopCredentialMaterializeResponse:
    """Deliver the selected credentials' values to an interactive Desktop client."""
    kind, client_id = client_claims
    if kind != "desktop" or not client_id:
        raise HTTPException(403, "Desktop authentication required")
    # A desktop JWT bought with an account CLI token must not gain secret export.
    ensure_not_cli_exchanged_session(session, client_id)
    try:
        result = await DesktopCredentialService.materialize(
            session, current_user, client_id, body
        )
    except DesktopCredentialError as e:
        raise HTTPException(e.status_code, e.message, headers=e.headers) from None
    response.headers["Cache-Control"] = "no-store"
    return result
