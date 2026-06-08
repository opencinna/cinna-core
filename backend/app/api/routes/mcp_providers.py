"""
MCP Provider — consumer-side connect helper + management.

Prefix: ``/api/v1/mcp-providers``

| Method | Path                    | Purpose                                            |
|--------|-------------------------|----------------------------------------------------|
| GET    | /discoverable-agents    | Platform agents the caller may consume (picker).   |
| POST   | /connect/agent          | Connect to a platform agent2agent connector.       |
| POST   | /connect/external       | Add an arbitrary external MCP server.              |
| GET    | /{credential_id}/status | Derived connection status (owner only).            |

Consuming a connection — including installing/using a shared one — is use-only
and available to every role (RD-7), so these routes gate on ownership/ACL only,
not on ``agent-developer``. Disconnect = ``DELETE /credentials/{id}`` (the bound
direct token cascade-deletes, revoking that consumer only).
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    ConnectMcpProviderAgentRequest,
    ConnectMcpProviderExternalRequest,
    DiscoverableAgents,
    MCPProviderConnectionResponse,
    MCPProviderOAuthAuthorizeResponse,
    MCPProviderOAuthCallbackRequest,
    MCPProviderOAuthCallbackResponse,
    MCPProviderStatus,
    MCPProviderTestResult,
)
from app.services.credentials.credentials_service import CredentialsService
from app.services.mcp_providers.mcp_provider_oauth_service import (
    MCPProviderOAuthError,
)
from app.services.mcp_providers.mcp_provider_service import (
    MCPProviderError,
    MCPProviderService,
)

router = APIRouter(prefix="/mcp-providers", tags=["mcp-providers"])


def _handle_error(e: MCPProviderError) -> None:
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/discoverable-agents", response_model=DiscoverableAgents)
def list_discoverable_agents(
    session: SessionDep,
    current_user: CurrentUser,
    consumer_agent_id: uuid.UUID | None = None,
) -> Any:
    """
    Platform agents that expose an agent2agent connector the current user is
    allowed to consume. Drives the "Connect MCP Provider → platform agent" picker.
    Excludes the consumer's own agent when ``consumer_agent_id`` is supplied.
    """
    agents = MCPProviderService.list_discoverable_agents(
        session, current_user, consumer_agent_id=consumer_agent_id
    )
    return DiscoverableAgents(data=agents, count=len(agents))


@router.post("/connect/agent", response_model=MCPProviderConnectionResponse)
async def connect_agent(
    body: ConnectMcpProviderAgentRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Connect to a platform agent's agent2agent MCP connector. Creates an
    ``mcp_provider`` credential, mints a bound direct token, and optionally links
    the credential to a consumer agent.
    """
    try:
        return await MCPProviderService.connect_to_agent(
            session, current_user, body, is_superuser=current_user.is_superuser
        )
    except MCPProviderError as e:
        _handle_error(e)


@router.post("/connect/external", response_model=MCPProviderConnectionResponse)
async def connect_external(
    body: ConnectMcpProviderExternalRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Add an arbitrary external MCP server (fixed_token / none create immediately;
    oauth_dcr creates an awaiting_auth credential — the live flow is Phase 5).
    """
    try:
        return await MCPProviderService.connect_to_external(
            session, current_user, body, is_superuser=current_user.is_superuser
        )
    except MCPProviderError as e:
        _handle_error(e)


@router.get("/{credential_id}/status", response_model=MCPProviderStatus)
def get_provider_status(
    credential_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Derived connection status for an ``mcp_provider`` credential. Owner-only."""
    try:
        return MCPProviderService.get_status(
            session, credential_id, current_user, is_superuser=current_user.is_superuser
        )
    except MCPProviderError as e:
        _handle_error(e)


# ── OAuth/DCR (Phase 5) ───────────────────────────────────────────────────────


@router.get(
    "/{credential_id}/oauth/authorize",
    response_model=MCPProviderOAuthAuthorizeResponse,
)
async def oauth_authorize(
    credential_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Begin (or re-run) the DCR + authorization flow for an ``oauth_dcr``
    credential. Performs DCR against the target AS (idempotent — reuses an
    existing client), builds the PKCE authorization URL, and returns it for the
    frontend to open. Owner-only.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
    )

    try:
        credential = MCPProviderService.get_owned_credential(
            session, credential_id, current_user,
            is_superuser=current_user.is_superuser,
        )
        authorize_url = await MCPProviderOAuthService.begin_authorization(
            session, credential, current_user.id
        )
        return MCPProviderOAuthAuthorizeResponse(authorize_url=authorize_url)
    except MCPProviderError as e:
        _handle_error(e)
    except MCPProviderOAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post(
    "/{credential_id}/oauth/reauthorize",
    response_model=MCPProviderOAuthAuthorizeResponse,
)
async def oauth_reauthorize(
    credential_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Re-run the authorization flow (revoked refresh token / scope change). Same
    surface as authorize; separate verb for an explicit user-initiated re-auth.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
    )

    try:
        credential = MCPProviderService.get_owned_credential(
            session, credential_id, current_user,
            is_superuser=current_user.is_superuser,
        )
        authorize_url = await MCPProviderOAuthService.begin_authorization(
            session, credential, current_user.id
        )
        return MCPProviderOAuthAuthorizeResponse(authorize_url=authorize_url)
    except MCPProviderError as e:
        _handle_error(e)
    except MCPProviderOAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post(
    "/oauth/callback", response_model=MCPProviderOAuthCallbackResponse
)
async def oauth_callback(
    body: MCPProviderOAuthCallbackRequest,
    session: SessionDep,
) -> Any:
    """
    Authorization-code callback. Validates state, exchanges the code (PKCE) for
    tokens, stores them encrypted, and triggers a credential-updated sync so the
    fresh access token reaches the consumer agent's running containers.

    Public (no auth dependency): the CSRF ``state`` token — minted owner-scoped
    in ``begin_authorization`` and single-use — is the authorization, exactly
    like the Google credential OAuth callback.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
    )

    try:
        credential = await MCPProviderOAuthService.handle_callback(
            session, body.code, body.state
        )
    except MCPProviderOAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    # Sync the fresh access token into affected agents' running containers.
    await CredentialsService.event_credential_updated(
        session=session, credential_id=credential.id
    )

    return MCPProviderOAuthCallbackResponse(
        credential_id=credential.id,
        status="connected",
        message="MCP provider authorization successful",
    )


@router.post("/{credential_id}/test", response_model=MCPProviderTestResult)
async def test_connection(
    credential_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Best-effort connectivity probe: open an MCP ``initialize`` + ``tools/list``
    against the endpoint with the current token and report the tool names (or an
    error). Owner-only; runs through the egress guard (RD-6).
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
    )

    try:
        credential = MCPProviderService.get_owned_credential(
            session, credential_id, current_user,
            is_superuser=current_user.is_superuser,
        )
    except MCPProviderError as e:
        _handle_error(e)
        return  # unreachable (_handle_error raises) — satisfies static analysis

    result = await MCPProviderOAuthService.probe(session, credential)
    return MCPProviderTestResult(
        ok=result.get("ok", False),
        tools=result.get("tools", []),
        error=result.get("error"),
    )
