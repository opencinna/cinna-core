"""Account-scoped, audited credential delivery to authenticated Desktop devices.

Business rules live in ``DesktopCredentialService``; this module only maps
HTTP (auth claims, ETag/cache headers, errors) onto it.
"""

from fastapi import APIRouter, HTTPException, Query, Request, Response

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

INCLUDE_AGENT_API_KEYS = "agent_api_keys"


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
    kind, client_id = client_claims
    if kind != "desktop" or not client_id:
        raise HTTPException(403, "Desktop authentication required")
    # A desktop JWT bought with an account CLI token must not gain secret export.
    ensure_not_cli_exchanged_session(session, client_id)
    try:
        result = await DesktopCredentialService.materialize(
            session,
            current_user,
            client_id,
            body,
            include_agent_api_keys=_includes_agent_api_keys(include),
        )
    except DesktopCredentialError as e:
        raise HTTPException(e.status_code, e.message, headers=e.headers) from None
    response.headers["Cache-Control"] = "no-store"
    return result
