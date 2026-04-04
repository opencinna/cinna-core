"""
Access Token API Routes - CRUD for A2A access tokens.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentAccessTokenCreate,
    AgentAccessTokenUpdate,
    AgentAccessTokenPublic,
    AgentAccessTokenCreated,
    AgentAccessTokensPublic,
    Message,
)
from app.services.a2a.access_token_service import AccessTokenService

router = APIRouter(prefix="/agents/{agent_id}/access-tokens", tags=["access-tokens"])


@router.get("/", response_model=AgentAccessTokensPublic)
def list_access_tokens(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    List all access tokens for an agent.
    """
    try:
        tokens = AccessTokenService.get_agent_tokens(
            session, agent_id, current_user.id
        )
        return AgentAccessTokensPublic(data=tokens, count=len(tokens))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/", response_model=AgentAccessTokenCreated)
def create_access_token(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    token_in: AgentAccessTokenCreate,
) -> Any:
    """
    Create a new access token for an agent.

    IMPORTANT: The token value is only returned once on creation.
    Store it securely - it cannot be retrieved later.
    """
    # Ensure agent_id in path matches body
    if token_in.agent_id != agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id in path must match body"
        )

    try:
        token = AccessTokenService.create_token(
            session, current_user.id, token_in
        )
        return token
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{token_id}", response_model=AgentAccessTokenPublic)
def get_access_token(
    agent_id: uuid.UUID,
    token_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Get access token by ID.
    """
    token = AccessTokenService.get_token_by_id(
        session, token_id, current_user.id
    )
    if not token:
        raise HTTPException(status_code=404, detail="Access token not found")
    if token.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Access token not found")
    return token


@router.put("/{token_id}", response_model=AgentAccessTokenPublic)
def update_access_token(
    agent_id: uuid.UUID,
    token_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    token_in: AgentAccessTokenUpdate,
) -> Any:
    """
    Update access token (name and revoked status only).
    """
    token = AccessTokenService.update_token(
        session, token_id, current_user.id, token_in
    )
    if not token:
        raise HTTPException(status_code=404, detail="Access token not found")
    if token.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Access token not found")
    return token


@router.delete("/{token_id}")
def delete_access_token(
    agent_id: uuid.UUID,
    token_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """
    Delete an access token.

    Note: Existing sessions created with this token will retain their
    access_token_id reference but the token will no longer be usable.
    """
    # First verify the token exists and belongs to the agent
    token = AccessTokenService.get_token_by_id(
        session, token_id, current_user.id
    )
    if not token or token.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Access token not found")

    success = AccessTokenService.delete_token(
        session, token_id, current_user.id
    )
    if not success:
        raise HTTPException(status_code=404, detail="Access token not found")
    return Message(message="Access token deleted successfully")
