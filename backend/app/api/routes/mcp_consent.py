from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.services.mcp.mcp_consent_service import MCPConsentService
from app.services.mcp.mcp_errors import MCPError

router = APIRouter(prefix="/mcp/consent", tags=["mcp-consent"])


class ConsentInfo(BaseModel):
    """Non-sensitive info displayed on the consent page."""
    agent_name: str
    connector_name: str
    connector_mode: str
    client_name: str
    scopes: list[str]
    expires_at: datetime


class ConsentApproveResponse(BaseModel):
    redirect_url: str


def _handle_mcp_error(e: MCPError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/{nonce}", response_model=ConsentInfo)
def get_consent_info(
    session: SessionDep,
    nonce: str,
) -> Any:
    """Fetch auth request details for the consent page. Public endpoint."""
    try:
        details = MCPConsentService.get_consent_details(session, nonce)
    except MCPError as e:
        _handle_mcp_error(e)

    return ConsentInfo(
        agent_name=details.agent_name,
        connector_name=details.connector_name,
        connector_mode=details.connector_mode,
        client_name=details.client_name,
        scopes=details.scopes,
        expires_at=details.expires_at,
    )


@router.post("/{nonce}/approve", response_model=ConsentApproveResponse)
def approve_consent(
    session: SessionDep,
    current_user: CurrentUser,
    nonce: str,
) -> Any:
    """User approves the OAuth consent. Requires JWT auth."""
    try:
        redirect_url = MCPConsentService.approve_consent(
            session, nonce, current_user,
        )
    except MCPError as e:
        _handle_mcp_error(e)

    return ConsentApproveResponse(redirect_url=redirect_url)
