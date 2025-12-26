import os
import logging
import json
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from datetime import datetime
from typing import Annotated

from .models import HealthCheckResponse, ChatRequest, ChatResponse, AgentPromptsResponse, AgentPromptsUpdate, CredentialsUpdate
from .sdk_manager import sdk_manager
from .agent_env_service import AgentEnvService

router = APIRouter(tags=["agent"])
logger = logging.getLogger(__name__)

# Environment variables (set from .env file via docker-compose)
ENV_ID = os.getenv("ENV_ID", "unknown")
AGENT_ID = os.getenv("AGENT_ID", "unknown")
ENV_NAME = os.getenv("ENV_NAME", "unknown")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")
WORKSPACE_DIR = os.getenv("CLAUDE_CODE_WORKSPACE", "/app/app")

# Initialize agent environment service
agent_env_service = AgentEnvService(WORKSPACE_DIR)


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


@router.post("/config/settings", dependencies=[Depends(verify_auth_token)])
async def set_config(config: dict):
    """Set agent configuration"""
    # TODO: Implement configuration storage
    return {"status": "ok"}


@router.post("/chat", dependencies=[Depends(verify_auth_token)])
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Handle chat messages.

    Routes to appropriate SDK based on agent_sdk parameter:
    - claude: Claude SDK (supports both building and conversation modes)
    """
    if request.agent_sdk == "claude":
        # Use Claude SDK for both building and conversation modes
        response_content = []
        new_session_id = request.session_id

        async for chunk in sdk_manager.send_message_stream(
            message=request.message,
            session_id=request.session_id,
            system_prompt=request.system_prompt,  # Only use explicit override if provided
            mode=request.mode,
            agent_sdk=request.agent_sdk,
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
            metadata={"mode": request.mode, "sdk": request.agent_sdk}
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported agent_sdk: {request.agent_sdk}. Currently only 'claude' is supported."
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
    if request.agent_sdk == "claude":
        logger.info(f"Starting stream for mode={request.mode}, sdk={request.agent_sdk}, session_id={request.session_id}, message={request.message[:50]}...")

        async def event_stream():
            """Generate SSE events from SDK stream"""
            event_count = 0
            try:
                logger.info("Calling sdk_manager.send_message_stream()...")
                async for chunk in sdk_manager.send_message_stream(
                    message=request.message,
                    session_id=request.session_id,
                    system_prompt=request.system_prompt,  # Only use explicit override if provided
                    mode=request.mode,
                    agent_sdk=request.agent_sdk,
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
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported agent_sdk: {request.agent_sdk}. Currently only 'claude' is supported."
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


@router.get("/config/agent-prompts", dependencies=[Depends(verify_auth_token)])
async def get_agent_prompts() -> AgentPromptsResponse:
    """
    Get current agent prompts from docs files.

    Returns the content of:
    - ./docs/WORKFLOW_PROMPT.md
    - ./docs/ENTRYPOINT_PROMPT.md

    These files are maintained by the building agent and define how the
    workflow should operate in conversation mode.
    """
    workflow_prompt, entrypoint_prompt = agent_env_service.get_agent_prompts()

    return AgentPromptsResponse(
        workflow_prompt=workflow_prompt,
        entrypoint_prompt=entrypoint_prompt
    )


@router.post("/config/agent-prompts", dependencies=[Depends(verify_auth_token)])
async def update_agent_prompts(prompts: AgentPromptsUpdate):
    """
    Update agent prompts in docs files.

    Updates the content of:
    - ./docs/WORKFLOW_PROMPT.md (if workflow_prompt is provided)
    - ./docs/ENTRYPOINT_PROMPT.md (if entrypoint_prompt is provided)

    This is used by the backend to sync manually edited prompts to the agent environment.
    """
    try:
        updated_files = agent_env_service.update_agent_prompts(
            workflow_prompt=prompts.workflow_prompt,
            entrypoint_prompt=prompts.entrypoint_prompt
        )

        return {
            "status": "ok",
            "message": f"Updated {len(updated_files)} file(s)",
            "updated_files": updated_files
        }
    except IOError as e:
        logger.error(f"Failed to update agent prompts: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update agent prompts: {str(e)}"
        )


@router.post("/config/credentials", dependencies=[Depends(verify_auth_token)])
async def update_credentials(credentials: CredentialsUpdate):
    """
    Update credentials in workspace credentials directory.

    Creates two files:
    - ./credentials/credentials.json (full credentials data)
    - ./credentials/README.md (redacted documentation for agent prompt)

    This is called by the backend when:
    - Environment starts (sync credentials)
    - User updates credentials
    """
    try:
        updated_files = agent_env_service.update_credentials(
            credentials_json=credentials.credentials_json,
            credentials_readme=credentials.credentials_readme
        )

        return {
            "status": "ok",
            "message": f"Updated {len(updated_files)} file(s)",
            "updated_files": updated_files
        }
    except IOError as e:
        logger.error(f"Failed to update credentials: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update credentials: {str(e)}"
        )
