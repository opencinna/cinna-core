import os
import re
import logging
import json
import asyncio
import mimetypes
import subprocess
from email.utils import formatdate
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status, File, UploadFile, Form
from fastapi.responses import StreamingResponse, Response, FileResponse
from datetime import datetime
from typing import Annotated, Any
from pathlib import Path

try:
    import httpx as _httpx  # available in env container
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from pydantic import BaseModel as _BaseModel

from .models import (
    HealthCheckResponse,
    ChatRequest,
    ChatResponse,
    AgentPromptsResponse,
    AgentPromptsUpdate,
    AgentHandoverUpdate,
    AgentHandoverResponse,
    CredentialsUpdate,
    WorkspaceTreeResponse,
    FileUploadResponse,
    DatabaseTableEntry,
    SQLiteDatabaseSchema,
    SQLiteQueryRequest,
    SQLiteQueryResult,
    PluginsUpdate,
    PluginsSettingsResponse,
)
from .sdk_manager import sdk_manager
from .agent_env_service import AgentEnvService
from .active_session_manager import active_session_manager

router = APIRouter(tags=["agent"])
logger = logging.getLogger(__name__)

# Import CredentialGuard singleton (Phase 2 — output redaction)
try:
    from .security.credential_guard import credential_guard as _credential_guard
    _CREDENTIAL_GUARD_AVAILABLE = True
except ImportError:
    _credential_guard = None  # type: ignore[assignment]
    _CREDENTIAL_GUARD_AVAILABLE = False


# ── Security proxy models ─────────────────────────────────────────────────────

class _SecurityEventReport(_BaseModel):
    """Payload for the security event proxy endpoint."""
    event_type: str
    tool_name: str | None = None
    tool_input: str | None = None
    session_id: str | None = None
    environment_id: str | None = None
    agent_id: str | None = None
    severity: str = "high"
    details: dict = {}


class _SecurityEventResponse(_BaseModel):
    """Response returned to the SDK hook."""
    action: str = "allow"
    reason: str | None = None


# ── Output redaction helper ───────────────────────────────────────────────────

async def _redacted_event_stream(source_generator, session_id: str | None = None):
    """
    Wrap an SSE event generator and redact known credential values from agent output.

    Scans SSE events of type "assistant" and "tool" for sensitive values tracked
    by the CredentialGuard singleton. Matching values are replaced with
    ***REDACTED*** before the event reaches the client.

    When a redaction occurs, an async fire-and-forget security event is reported
    to the backend via the /security/report proxy.

    Args:
        source_generator: Async generator yielding raw SSE strings ("data: {...}\n\n")
        session_id: Backend session ID for security event attribution (optional)
    """
    # Types of SSE events whose content should be scanned
    REDACT_EVENT_TYPES = {"assistant", "tool"}

    async for sse_line in source_generator:
        if not _CREDENTIAL_GUARD_AVAILABLE or _credential_guard is None:
            yield sse_line
            continue

        # SSE format: "data: <json>\n\n" — extract the JSON payload
        if not sse_line.startswith("data: "):
            yield sse_line
            continue

        try:
            payload_str = sse_line[len("data: "):].rstrip()
            chunk = json.loads(payload_str)
        except (json.JSONDecodeError, ValueError):
            yield sse_line
            continue

        event_type = chunk.get("type", "")
        content = chunk.get("content", "")

        if event_type in REDACT_EVENT_TYPES and content:
            redacted_content, was_redacted = _credential_guard.redact(content)
            if was_redacted:
                chunk["content"] = redacted_content
                sse_line = f"data: {json.dumps(chunk)}\n\n"
                # Fire-and-forget report — does not block the stream
                try:
                    from .security.event_reporter import SecurityEventReporter
                    _reporter = SecurityEventReporter()
                    asyncio.create_task(
                        _reporter.report_async(
                            event_type="OUTPUT_REDACTED",
                            session_id=session_id,
                            severity="medium",
                            details={"event_subtype": event_type},
                        )
                    )
                except Exception:
                    pass  # Non-critical — redaction still happened

        yield sse_line

# Environment variables (set from .env file via docker-compose)
ENV_ID = os.getenv("ENV_ID", "unknown")
AGENT_ID = os.getenv("AGENT_ID", "unknown")
ENV_NAME = os.getenv("ENV_NAME", "unknown")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")
WORKSPACE_DIR = os.getenv("CLAUDE_CODE_WORKSPACE", "/app/workspace")

# Initialize agent environment service
agent_env_service = AgentEnvService(WORKSPACE_DIR)


async def _store_session_context(request: ChatRequest) -> None:
    """Store session context from the request into the active session manager.

    Handles both the legacy single-context API and the new per-session
    HMAC-verified context storage.
    """
    if not (request.session_state and "session_context" in request.session_state):
        return

    context_data = {
        **request.session_state["session_context"],
        "session_id": request.session_id,
        "backend_session_id": request.backend_session_id,
        "mode": request.mode,
    }
    # Legacy single-context (backward compat)
    await active_session_manager.set_current_context(context_data)
    # Per-session context with HMAC verification
    if request.backend_session_id:
        await active_session_manager.set_session_context(
            backend_session_id=request.backend_session_id,
            context=request.session_state["session_context"],
            signature=request.session_state.get("session_context_signature"),
        )


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


@router.post("/security/report")
async def proxy_security_event(event: _SecurityEventReport) -> _SecurityEventResponse:
    """
    Proxy a security event from an SDK hook to the backend blockable endpoint.

    Called by credential_guard_hook.py (Claude Code hook) and ADK inline
    interceptors. Does NOT require the AGENT_AUTH_TOKEN header — this endpoint
    is on localhost and only accessible inside the container.

    Forwards to backend POST /api/v1/security-events/report with the
    AGENT_AUTH_TOKEN attached. Returns the action decision to the caller.

    Fail-open: if the backend is unreachable or times out, returns action="allow".
    """
    if not _HTTPX_AVAILABLE:
        logger.warning("httpx not available — security event proxy returning allow")
        return _SecurityEventResponse(action="allow")

    backend_url = os.getenv("BACKEND_URL", "http://host.docker.internal:8000")
    auth_token = AGENT_AUTH_TOKEN

    if not auth_token:
        logger.warning("AGENT_AUTH_TOKEN not set — security event proxy returning allow")
        return _SecurityEventResponse(action="allow")

    try:
        async with _httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(
                f"{backend_url}/api/v1/security-events/report",
                json=event.model_dump(),
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            response.raise_for_status()
            data = response.json()
            return _SecurityEventResponse(
                action=data.get("action", "allow"),
                reason=data.get("reason"),
            )
    except Exception as exc:
        logger.warning(
            "Backend unreachable for security event proxy (fail-open): %s", exc
        )
        return _SecurityEventResponse(action="allow")


@router.post("/config/settings", dependencies=[Depends(verify_auth_token)])
async def set_config(config: dict):
    """Set agent configuration"""
    # TODO: Implement configuration storage
    return {"status": "ok"}


@router.post("/chat", dependencies=[Depends(verify_auth_token)])
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Handle chat messages.

    Uses Claude SDK for both building and conversation modes.
    SDK configuration (Anthropic vs MiniMax) is determined by settings files in the environment.
    """
    await _store_session_context(request)

    response_content = []
    new_session_id = request.session_id

    try:
        async for chunk in sdk_manager.send_message_stream(
            message=request.message,
            session_id=request.session_id,
            backend_session_id=request.backend_session_id,
            system_prompt=request.system_prompt,  # Only use explicit override if provided
            mode=request.mode,
            session_state=request.session_state,
        ):
            # Capture session ID from session_created event
            if chunk["type"] == "session_created":
                new_session_id = chunk["session_id"]

            # Collect content
            if chunk.get("content"):
                response_content.append(chunk["content"])
    finally:
        await active_session_manager.clear_context()
        if request.backend_session_id:
            await active_session_manager.cleanup_session_context(request.backend_session_id)

    return ChatResponse(
        response="\n".join(response_content),
        session_id=new_session_id,
        metadata={"mode": request.mode}
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

    SDK configuration (Anthropic vs MiniMax) is determined by settings files in the environment.
    """
    logger.info(f"Starting stream for mode={request.mode}, sdk_session_id={request.session_id}, backend_session_id={request.backend_session_id}, message={request.message[:50]}...")

    await _store_session_context(request)

    async def event_stream():
        """Generate SSE events from SDK stream"""
        event_count = 0
        session_id_for_cleanup = request.session_id
        try:
            logger.info("Calling sdk_manager.send_message_stream()...")
            async for chunk in sdk_manager.send_message_stream(
                message=request.message,
                session_id=request.session_id,
                backend_session_id=request.backend_session_id,
                system_prompt=request.system_prompt,  # Only use explicit override if provided
                mode=request.mode,
                session_state=request.session_state,
            ):
                event_count += 1
                logger.info(f"[Stream event #{event_count}] Received chunk type={chunk.get('type')}, content_length={len(chunk.get('content', ''))}")

                # Capture session_id for cleanup if this is a new session
                if chunk.get("type") == "session_created" and chunk.get("session_id"):
                    session_id_for_cleanup = chunk.get("session_id")

                # Format as SSE event
                event_data = json.dumps(chunk)
                try:
                    yield f"data: {event_data}\n\n"
                except (ConnectionResetError, BrokenPipeError) as conn_error:
                    logger.warning(f"Client disconnected while streaming (event #{event_count}): {conn_error}")
                    # Backend disconnected - SDK manager will continue and clean up in finally block
                    # We can't send more data, so break the loop
                    break

            logger.info(f"SDK stream completed. Total events: {event_count}")

            # Send done event
            logger.info("Sending final done event")
            try:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            except (ConnectionResetError, BrokenPipeError) as conn_error:
                logger.warning(f"Client disconnected before done event: {conn_error}")

        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            error_event = json.dumps({
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__,
            })
            try:
                yield f"data: {error_event}\n\n"
            except (ConnectionResetError, BrokenPipeError):
                logger.warning("Client disconnected before error event could be sent")
        finally:
            await active_session_manager.clear_context()
            if request.backend_session_id:
                await active_session_manager.cleanup_session_context(request.backend_session_id)

    return StreamingResponse(
        _redacted_event_stream(event_stream(), session_id=request.backend_session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@router.post("/chat/interrupt/{session_id}", dependencies=[Depends(verify_auth_token)])
async def interrupt_session(session_id: str):
    """
    Request interrupt for an active streaming session.

    This sets an interrupt flag that will be checked during message streaming.
    The actual SDK interrupt() is called from within the streaming loop.

    Args:
        session_id: External SDK session ID

    Returns:
        Status indicating if interrupt was requested
    """
    logger.info(f"Interrupt request received for session: {session_id}")

    success = await active_session_manager.request_interrupt(session_id)

    if success:
        return {
            "status": "ok",
            "message": f"Interrupt requested for session {session_id}",
            "session_id": session_id
        }
    else:
        # Session not found or not active - this is OK, might have just finished
        logger.info(f"Interrupt request for inactive session {session_id} (may have completed)")
        return {
            "status": "not_found",
            "message": f"Session {session_id} is not currently active (may have completed)",
            "session_id": session_id
        }


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


@router.get("/session/context")
async def get_session_context(session_id: str | None = Query(default=None)):
    """
    Get session context metadata.

    This endpoint is available without authentication (localhost-only, internal to container).
    Agent scripts can call this to determine if the current session is email-initiated,
    whether they're running as a clone, and what the parent agent is.

    Args:
        session_id: Backend session ID to look up specific session context.
                    If provided, returns HMAC-verified per-session context.
                    If omitted, falls back to legacy current-session context.

    Returns:
        Session context dict with integration_type, agent_id, is_clone, parent_agent_id, etc.
        Returns 404 if session_id is provided but not found.
        Returns empty context if no session is currently active (legacy mode).
    """
    # Per-session lookup (preferred path)
    if session_id:
        context = await active_session_manager.get_session_context(session_id)
        if context:
            return context
        raise HTTPException(status_code=404, detail=f"No context found for session_id={session_id}")

    # Legacy fallback: return single current context
    context = await active_session_manager.get_current_context()
    if context:
        return context
    return {
        "session_id": None,
        "backend_session_id": None,
        "integration_type": None,
        "agent_id": None,
        "is_clone": False,
        "parent_agent_id": None,
        "mode": None,
    }


@router.get("/config/agent-prompts", dependencies=[Depends(verify_auth_token)])
async def get_agent_prompts() -> AgentPromptsResponse:
    """
    Get current agent prompts from docs files.

    Returns the content of:
    - ./docs/WORKFLOW_PROMPT.md
    - ./docs/ENTRYPOINT_PROMPT.md
    - ./docs/REFINER_PROMPT.md

    These files are maintained by the building agent and define how the
    workflow should operate in conversation mode.
    """
    workflow_prompt, entrypoint_prompt, refiner_prompt = agent_env_service.get_agent_prompts()

    return AgentPromptsResponse(
        workflow_prompt=workflow_prompt,
        entrypoint_prompt=entrypoint_prompt,
        refiner_prompt=refiner_prompt
    )


@router.post("/config/agent-prompts", dependencies=[Depends(verify_auth_token)])
async def update_agent_prompts(prompts: AgentPromptsUpdate):
    """
    Update agent prompts in docs files.

    Updates the content of:
    - ./docs/WORKFLOW_PROMPT.md (if workflow_prompt is provided)
    - ./docs/ENTRYPOINT_PROMPT.md (if entrypoint_prompt is provided)
    - ./docs/REFINER_PROMPT.md (if refiner_prompt is provided)

    This is used by the backend to sync manually edited prompts to the agent environment.
    """
    try:
        updated_files = agent_env_service.update_agent_prompts(
            workflow_prompt=prompts.workflow_prompt,
            entrypoint_prompt=prompts.entrypoint_prompt,
            refiner_prompt=prompts.refiner_prompt
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


@router.get("/config/agent-handovers", dependencies=[Depends(verify_auth_token)])
async def get_agent_handover_config() -> AgentHandoverResponse:
    """
    Get current agent handover configuration from JSON file.

    Returns the content of ./docs/agent_handover_config.json containing:
    - handovers: Array of {id, name, prompt} objects for configured handovers
    - handover_prompt: Instructions to append to conversation mode system prompt
    """
    config = agent_env_service.get_agent_handover_config()

    return AgentHandoverResponse(
        handovers=config.get("handovers", []),
        handover_prompt=config.get("handover_prompt", "")
    )


@router.post("/config/agent-handovers", dependencies=[Depends(verify_auth_token)])
async def update_agent_handover_config(config: AgentHandoverUpdate):
    """
    Update agent handover configuration in JSON file.

    Updates ./docs/agent_handover_config.json with:
    - handovers: Array of configured handover targets (id, name, prompt)
    - handover_prompt: Instructions for using create_agent_task tool in conversation mode

    This is called by the backend when user updates handover configurations in the UI.
    The handover_prompt is appended to the conversation mode system prompt, and the
    handovers list is used by the create_agent_task tool to validate direct handover targets.
    """
    try:
        agent_env_service.update_agent_handover_config(
            handovers=config.handovers,
            handover_prompt=config.handover_prompt
        )

        return {
            "status": "ok",
            "message": f"Updated agent handover config with {len(config.handovers)} handover(s)"
        }
    except IOError as e:
        logger.error(f"Failed to update agent handover config: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update agent handover config: {str(e)}"
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
            credentials_readme=credentials.credentials_readme,
            service_account_files=credentials.service_account_files
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


@router.post("/config/plugins", dependencies=[Depends(verify_auth_token)])
async def update_plugins(plugins: PluginsUpdate):
    """
    Update plugins in workspace plugins directory.

    Creates the following structure:
    ./plugins/
    ├── settings.json (active plugins configuration)
    └── [marketplace_name]/
        └── [plugin_name]/
            └── (plugin files)

    This is called by the backend when:
    - Environment starts (sync plugins)
    - User installs/updates/uninstalls plugins
    """
    try:
        # Convert PluginInfo models to dicts for internal use
        active_plugins_dicts = [
            {
                "marketplace_name": p.marketplace_name,
                "plugin_name": p.plugin_name,
                "path": p.path,
                "conversation_mode": p.conversation_mode,
                "building_mode": p.building_mode,
                "version": p.version,
                "commit_hash": p.commit_hash,
            }
            for p in plugins.active_plugins
        ]

        updated_files = agent_env_service.update_plugins(
            active_plugins=active_plugins_dicts,
            settings_json=plugins.settings_json,
            plugin_files=plugins.plugin_files
        )

        return {
            "status": "ok",
            "message": f"Updated {len(updated_files)} file(s)",
            "updated_files": updated_files,
            "plugins_count": len(plugins.active_plugins)
        }
    except IOError as e:
        logger.error(f"Failed to update plugins: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update plugins: {str(e)}"
        )


@router.get("/config/plugins/settings", dependencies=[Depends(verify_auth_token)])
async def get_plugins_settings() -> PluginsSettingsResponse:
    """
    Get current plugins settings from settings.json.

    Returns the active plugins configuration including:
    - marketplace_name: Name of the marketplace
    - plugin_name: Name of the plugin
    - path: Full path to plugin in workspace
    - conversation_mode: Whether plugin is enabled for conversation mode
    - building_mode: Whether plugin is enabled for building mode
    """
    settings = agent_env_service.get_plugins_settings()

    # Convert to PluginInfo models
    from .models import PluginInfo

    active_plugins = [
        PluginInfo(
            marketplace_name=p.get("marketplace_name", ""),
            plugin_name=p.get("plugin_name", ""),
            path=p.get("path", ""),
            conversation_mode=p.get("conversation_mode", False),
            building_mode=p.get("building_mode", False),
            version=p.get("version"),
            commit_hash=p.get("commit_hash"),
        )
        for p in settings.get("active_plugins", [])
    ]

    return PluginsSettingsResponse(active_plugins=active_plugins)


@router.get("/workspace/tree", dependencies=[Depends(verify_auth_token)])
async def get_workspace_tree() -> WorkspaceTreeResponse:
    """
    Get complete workspace tree structure for files, logs, scripts, docs, uploads.

    Returns:
        JSON tree with all folders/files and folder summaries

    Response Example:
    {
      "files": {
        "name": "files",
        "type": "folder",
        "path": "files",
        "children": [...]
      },
      "logs": {...},
      "scripts": {...},
      "docs": {...},
      "uploads": {...},
      "summaries": {
        "files": {"fileCount": 42, "totalSize": 1048576},
        "logs": {"fileCount": 10, "totalSize": 204800},
        ...
      }
    }

    Error Handling:
    - 500: Workspace directory doesn't exist or not accessible
    """
    try:
        tree_data = agent_env_service.get_workspace_tree()
        return tree_data
    except IOError as e:
        logger.error(f"Failed to build workspace tree: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build workspace tree: {str(e)}"
        )


@router.get("/workspace/download/{path:path}", dependencies=[Depends(verify_auth_token)])
async def download_workspace_item(path: str):
    """
    Download a file or folder from workspace.

    Args:
        path: Relative path from workspace root (e.g., "files/data.csv" or "logs")

    Returns:
        - For files: StreamingResponse with file content
        - For folders: StreamingResponse with zip archive

    Headers:
    - Content-Disposition: attachment; filename="..."
    - Content-Type: application/octet-stream (file) or application/zip (folder)

    Security:
    - Path validation via validate_workspace_path()
    - Prevents directory traversal attacks
    - Rejects paths outside workspace

    Error Handling:
    - 400: Invalid path (contains .., absolute, etc.)
    - 404: Path doesn't exist
    - 500: I/O error during zip creation or file read
    """
    try:
        # Validate path
        absolute_path = agent_env_service.validate_workspace_path(path)

        if not absolute_path.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        if absolute_path.is_file():
            # Stream file directly
            filename = absolute_path.name

            async def file_stream():
                with open(absolute_path, 'rb') as f:
                    while chunk := f.read(65536):  # 64KB chunks
                        yield chunk

            return StreamingResponse(
                file_stream(),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(absolute_path.stat().st_size)
                }
            )
        else:
            # Create zip and stream
            zip_path = agent_env_service.create_workspace_zip(path)
            folder_name = absolute_path.name or "workspace"

            async def zip_stream():
                try:
                    with open(zip_path, 'rb') as f:
                        while chunk := f.read(65536):  # 64KB chunks
                            yield chunk
                finally:
                    # Clean up temp zip file
                    if zip_path.exists():
                        zip_path.unlink()

            return StreamingResponse(
                zip_stream(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{folder_name}.zip"',
                    "Content-Length": str(zip_path.stat().st_size)
                }
            )

    except ValueError as e:
        # Path validation error
        raise HTTPException(status_code=400, detail=f"Invalid path: {str(e)}")
    except IOError as e:
        logger.error(f"Failed to download {path}: {e}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


@router.post("/files/upload", response_model=FileUploadResponse, dependencies=[Depends(verify_auth_token)])
async def upload_file_to_workspace(
    file: UploadFile = File(...),
    filename: str = Form(...),
) -> FileUploadResponse:
    """
    Receive file from backend and store to workspace/uploads/ directory.

    Request:
    - file: Binary file content (multipart/form-data)
    - filename: Suggested filename (will be sanitized)
    - auth_token: Bearer token for authentication

    Response:
    {
        "path": "./uploads/document.pdf",
        "filename": "document.pdf",
        "size": 1234567,
        "message": "File uploaded successfully"
    }

    Security:
    - Validates auth token
    - Sanitizes filename (prevent directory traversal)
    - Handles filename conflicts (append _1, _2, etc.)
    """
    # Sanitize filename
    safe_filename = AgentEnvService.sanitize_filename(filename)

    # Ensure uploads directory exists
    uploads_dir = Path("/app/workspace/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Handle filename conflicts
    final_filename = AgentEnvService.resolve_filename_conflict(safe_filename, uploads_dir)

    # Target path
    target_path = uploads_dir / final_filename

    # Write file
    content = await file.read()
    target_path.write_bytes(content)

    # Return relative path (what agent will see)
    return FileUploadResponse(
        path=f"./uploads/{final_filename}",
        filename=final_filename,
        size=len(content),
        message="File uploaded successfully"
    )


# SQLite Database Endpoints

@router.get("/database/tables/{path:path}", dependencies=[Depends(verify_auth_token)])
async def get_database_tables(path: str) -> list[DatabaseTableEntry]:
    """
    Get list of tables and views from SQLite database.

    Args:
        path: Relative path to SQLite file from workspace root

    Returns:
        List of table/view entries with name and type

    Raises:
        400: Invalid path
        404: File not found
        500: Database error
    """
    try:
        tables = agent_env_service.get_database_tables(path)
        return [DatabaseTableEntry(**t) for t in tables]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IOError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting database tables for {path}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get tables: {str(e)}")


@router.get("/database/schema/{path:path}", dependencies=[Depends(verify_auth_token)])
async def get_database_schema(path: str) -> SQLiteDatabaseSchema:
    """
    Get complete schema for SQLite database.

    Args:
        path: Relative path to SQLite file from workspace root

    Returns:
        Database schema with tables, views, and columns

    Raises:
        400: Invalid path
        404: File not found
        500: Database error
    """
    try:
        schema = agent_env_service.get_database_schema(path)
        return SQLiteDatabaseSchema(**schema)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IOError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting database schema for {path}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get schema: {str(e)}")


@router.post("/database/query", dependencies=[Depends(verify_auth_token)])
async def execute_database_query(request: SQLiteQueryRequest) -> SQLiteQueryResult:
    """
    Execute SQL query on SQLite database.

    For SELECT queries: returns paginated results
    For DML queries (INSERT/UPDATE/DELETE): returns rows_affected count

    Args:
        request: Query request with path, SQL, pagination, and timeout

    Returns:
        Query result with columns, rows, pagination info, and execution stats
    """
    try:
        result = agent_env_service.execute_query(
            relative_path=request.path,
            query=request.query,
            page=request.page,
            page_size=request.page_size,
            timeout_seconds=request.timeout_seconds
        )
        return SQLiteQueryResult(**result)
    except ValueError as e:
        # Path validation error
        return SQLiteQueryResult(
            columns=[],
            rows=[],
            total_rows=0,
            page=request.page,
            page_size=request.page_size,
            has_more=False,
            execution_time_ms=0,
            query_type="OTHER",
            rows_affected=None,
            error=str(e),
            error_type="file_error"
        )
    except Exception as e:
        logger.error(f"Error executing query on {request.path}: {e}")
        return SQLiteQueryResult(
            columns=[],
            rows=[],
            total_rows=0,
            page=request.page,
            page_size=request.page_size,
            has_more=False,
            execution_time_ms=0,
            query_type="OTHER",
            rows_affected=None,
            error=str(e),
            error_type="execution_error"
        )


# ── Webapp Endpoints ─────────────────────────────────────────────────────

WEBAPP_DIR = Path(WORKSPACE_DIR) / "webapp"
WEBAPP_SIZE_LIMIT_BYTES = 100 * 1024 * 1024  # 100MB
WEBAPP_FRAMEWORK_DIR = Path("/app/core/webapp-framework")


def _validate_webapp_path(path: str) -> Path:
    """Validate and resolve a webapp path, preventing traversal attacks."""
    # Reject obviously malicious patterns
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    resolved = (WEBAPP_DIR / path).resolve()
    if not str(resolved).startswith(str(WEBAPP_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    return resolved


@router.get("/webapp/status", dependencies=[Depends(verify_auth_token)])
async def get_webapp_status():
    """Return webapp metadata: exists, total size, file count, entry point, api endpoints."""
    webapp_dir = WEBAPP_DIR.resolve()
    if not webapp_dir.exists():
        return {
            "exists": False,
            "total_size_bytes": 0,
            "file_count": 0,
            "has_index": False,
            "api_endpoints": [],
        }

    total_size = 0
    file_count = 0
    for f in webapp_dir.rglob("*"):
        if f.is_file():
            total_size += f.stat().st_size
            file_count += 1

    has_index = (webapp_dir / "index.html").is_file()

    api_endpoints = []
    api_dir = webapp_dir / "api"
    if api_dir.is_dir():
        for f in api_dir.iterdir():
            if f.is_file() and f.suffix == ".py" and f.name != "__init__.py":
                api_endpoints.append(f.stem)

    return {
        "exists": True,
        "total_size_bytes": total_size,
        "file_count": file_count,
        "has_index": has_index,
        "api_endpoints": sorted(api_endpoints),
    }


@router.post("/webapp/api/{endpoint}", dependencies=[Depends(verify_auth_token)])
async def webapp_data_api(endpoint: str, request: Request):
    """Execute a Python data script in webapp/api/{endpoint}.py and return JSON."""
    # Validate endpoint name: alphanumeric + underscores only
    if not re.match(r"^[a-zA-Z0-9_]+$", endpoint):
        raise HTTPException(status_code=400, detail="Invalid endpoint name")

    script_path = WEBAPP_DIR / "api" / f"{endpoint}.py"
    if not script_path.is_file():
        raise HTTPException(status_code=404, detail=f"Endpoint not found: {endpoint}")

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        body = {}

    params = body.get("params", {})
    timeout = min(body.get("timeout", 60), 300)

    stdin_data = json.dumps(params)

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "python", str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WEBAPP_DIR.parent),  # workspace root
            ),
            timeout=timeout + 2,  # small buffer for process creation
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Script timed out after {timeout}s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute script: {str(e)}")

    if proc.returncode != 0:
        return Response(
            content=json.dumps({
                "error": f"Script exited with code {proc.returncode}",
                "stderr": stderr.decode(errors="replace")[:2000],
            }),
            status_code=500,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    # Return stdout as JSON
    return Response(
        content=stdout,
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/webapp/{path:path}", dependencies=[Depends(verify_auth_token)])
async def serve_webapp_file(path: str, request: Request):
    """Serve a static file from webapp/ with caching headers."""
    if not path or path == "":
        path = "index.html"

    file_path = _validate_webapp_path(path)

    if not file_path.is_file():
        # Fallback: serve context-bridge.js from the framework directory if not in webapp
        if path == "assets/context-bridge.js":
            framework_file = WEBAPP_FRAMEWORK_DIR / "context-bridge.js"
            if framework_file.is_file():
                file_path = framework_file

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")

    stat = file_path.stat()
    mtime = stat.st_mtime
    size = stat.st_size
    etag = f'"{int(mtime)}-{size}"'
    last_modified = formatdate(mtime, usegmt=True)

    # Handle conditional requests
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Last-Modified": last_modified})

    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        from email.utils import parsedate_to_datetime
        try:
            ims_dt = parsedate_to_datetime(if_modified_since)
            file_dt = datetime.utcfromtimestamp(mtime)
            if file_dt <= ims_dt:
                return Response(status_code=304, headers={"ETag": etag, "Last-Modified": last_modified})
        except Exception:
            pass

    content_type, _ = mimetypes.guess_type(file_path.name)
    if content_type is None:
        content_type = "application/octet-stream"

    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        headers={
            "ETag": etag,
            "Last-Modified": last_modified,
            "Cache-Control": "no-cache",
        },
    )
