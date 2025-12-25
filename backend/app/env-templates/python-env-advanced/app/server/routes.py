import os
import logging
import json
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from datetime import datetime
from typing import Annotated

from .models import HealthCheckResponse, ChatRequest, ChatResponse, PromptsConfig
from .sdk_manager import sdk_manager

router = APIRouter(tags=["agent"])
logger = logging.getLogger(__name__)

# Environment variables (set from .env file via docker-compose)
ENV_ID = os.getenv("ENV_ID", "unknown")
AGENT_ID = os.getenv("AGENT_ID", "unknown")
ENV_NAME = os.getenv("ENV_NAME", "unknown")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")

# In-memory storage for prompts (will be set by backend)
_workflow_prompt = os.getenv("WORKFLOW_PROMPT", "")
_entrypoint_prompt = os.getenv("ENTRYPOINT_PROMPT", "")


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
        message=f"Agent server running (env={ENV_NAME}, env_id={ENV_ID})"
    )


@router.post("/config/prompts", dependencies=[Depends(verify_auth_token)])
async def set_prompts(prompts: PromptsConfig):
    """Set agent prompts"""
    global _workflow_prompt, _entrypoint_prompt

    if prompts.workflow_prompt is not None:
        _workflow_prompt = prompts.workflow_prompt

    if prompts.entrypoint_prompt is not None:
        _entrypoint_prompt = prompts.entrypoint_prompt

    logger.info("Prompts updated")
    return {"status": "ok", "message": "Prompts updated"}


@router.post("/config/settings", dependencies=[Depends(verify_auth_token)])
async def set_config(config: dict):
    """Set agent configuration"""
    # TODO: Implement configuration storage
    return {"status": "ok"}


@router.post("/chat", dependencies=[Depends(verify_auth_token)])
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Handle chat messages.

    Routes to appropriate handler based on mode:
    - building: Claude Code SDK
    - conversation: Google ADK (to be implemented)
    """
    if request.mode == "building":
        # Use Claude Code SDK with building mode (claude_code preset + BUILDING_AGENT.md)
        response_content = []
        new_session_id = request.session_id

        async for chunk in sdk_manager.send_message_stream(
            message=request.message,
            session_id=request.session_id,
            system_prompt=request.system_prompt,  # Only use explicit override if provided
            use_building_mode=True,
        ):
            # Capture session ID from session_created event
            if chunk["type"] == "session_created":
                new_session_id = chunk["session_id"]

            # Collect content
            if chunk.get("content"):
                response_content.append(chunk["content"])

        return ChatResponse(
            response="\n".join(response_content),
            session_id=new_session_id,
            metadata={"mode": "building", "sdk": "claude_code"}
        )

    elif request.mode == "conversation":
        # TODO: Implement Google ADK routing
        raise HTTPException(
            status_code=501,
            detail="Conversation mode not yet implemented"
        )

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {request.mode}"
        )


@router.post("/chat/stream", dependencies=[Depends(verify_auth_token)])
async def chat_stream(request: ChatRequest):
    """
    Stream chat responses using Server-Sent Events (SSE).

    Each event is a JSON object with:
    {
      "type": "assistant" | "tool" | "result" | "error" | "session_created",
      "content": str,
      "session_id": str,
      ...
    }
    """
    if request.mode == "building":
        logger.info(f"Starting stream for mode=building, session_id={request.session_id}, message={request.message[:50]}...")

        async def event_stream():
            """Generate SSE events from SDK stream"""
            event_count = 0
            try:
                logger.info("Calling sdk_manager.send_message_stream()...")
                async for chunk in sdk_manager.send_message_stream(
                    message=request.message,
                    session_id=request.session_id,
                    system_prompt=request.system_prompt,  # Only use explicit override if provided
                    use_building_mode=True,
                ):
                    event_count += 1
                    logger.info(f"[Stream event #{event_count}] Received chunk type={chunk.get('type')}, content_length={len(chunk.get('content', ''))}")

                    # Format as SSE event
                    event_data = json.dumps(chunk)
                    yield f"data: {event_data}\n\n"

                logger.info(f"SDK stream completed. Total events: {event_count}")

                # Send done event
                logger.info("Sending final done event")
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

            except Exception as e:
                logger.error(f"Stream error: {e}", exc_info=True)
                error_event = json.dumps({
                    "type": "error",
                    "content": str(e),
                    "error_type": type(e).__name__,
                })
                yield f"data: {error_event}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            }
        )

    elif request.mode == "conversation":
        raise HTTPException(
            status_code=501,
            detail="Conversation mode streaming not yet implemented"
        )

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {request.mode}"
        )


@router.get("/sdk/sessions", dependencies=[Depends(verify_auth_token)])
async def list_sdk_sessions():
    """List active SDK sessions (for debugging)"""
    # Note: Claude SDK client doesn't maintain persistent sessions
    # Each request creates a new client that connects/disconnects
    return {
        "message": "SDK sessions are created per-request and not tracked",
        "active_sessions": [],
        "count": 0
    }


@router.delete("/sdk/sessions/{session_id}", dependencies=[Depends(verify_auth_token)])
async def close_sdk_session(session_id: str):
    """Close an SDK session"""
    # Note: Claude SDK sessions are managed by resuming with session_id
    # No explicit close needed - they're stateless on server side
    return {
        "status": "ok",
        "message": f"Session {session_id} will be automatically cleaned up"
    }
