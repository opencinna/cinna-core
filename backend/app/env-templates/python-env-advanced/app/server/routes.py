import os
from fastapi import APIRouter, Depends, Header, HTTPException, status
from datetime import datetime
from typing import Annotated
from .models import HealthCheckResponse

router = APIRouter(tags=["agent"])

# Environment variables (set from .env file via docker-compose)
ENV_ID = os.getenv("ENV_ID", "unknown")
AGENT_ID = os.getenv("AGENT_ID", "unknown")
ENV_NAME = os.getenv("ENV_NAME", "unknown")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")


async def verify_auth_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """
    Verify the Authorization header contains the correct bearer token.

    Args:
        authorization: Authorization header value (e.g., "Bearer <token>")

    Raises:
        HTTPException: If token is missing or invalid
    """
    if not AGENT_AUTH_TOKEN:
        # If no auth token is configured, allow all requests (backward compatibility)
        return

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header"
        )

    # Expected format: "Bearer <token>"
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Expected 'Bearer'"
            )

        if token != AGENT_AUTH_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token"
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'"
        )


@router.get("/health")
async def health_check() -> HealthCheckResponse:
    """
    Health check endpoint.

    Used by:
    - Docker HEALTHCHECK
    - Backend EnvironmentAdapter health checks
    - Monitoring systems
    """
    return HealthCheckResponse(
        status="healthy",
        timestamp=datetime.utcnow(),
        uptime=0,  # TODO: Calculate actual uptime
        message=f"Agent server is running (env={ENV_NAME}, env_id={ENV_ID}, agent_id={AGENT_ID})"
    )


# Placeholder endpoints for future implementation
@router.post("/config/prompts", dependencies=[Depends(verify_auth_token)])
async def set_prompts(workflow_prompt: str | None = None, entrypoint_prompt: str | None = None):
    """Set agent prompts (to be implemented)"""
    return {"status": "not_implemented"}


@router.post("/config/settings", dependencies=[Depends(verify_auth_token)])
async def set_config(config: dict):
    """Set agent configuration (to be implemented)"""
    return {"status": "not_implemented"}


@router.post("/chat", dependencies=[Depends(verify_auth_token)])
async def chat(message: str):
    """
    Handle chat messages (to be implemented with Google ADK).

    This will be the main endpoint for agent communication.
    """
    return {"status": "not_implemented", "message": "Chat endpoint coming soon"}


@router.post("/chat/stream", dependencies=[Depends(verify_auth_token)])
async def chat_stream(message: str):
    """Stream chat responses (to be implemented)"""
    return {"status": "not_implemented"}
