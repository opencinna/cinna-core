import os
import re
import logging
import json
import asyncio
import mimetypes
import subprocess
from email.utils import formatdate
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, status, File, UploadFile, Form
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
    CommandStreamRequest,
)
from .sdk_manager import sdk_manager
from .agent_env_service import AgentEnvService
from .active_session_manager import active_session_manager
from .prompt_generator import PromptGenerator

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


def _build_extra_instructions_block(
    include_extra_instructions: str | None,
    extra_instructions_prepend: str | None,
) -> str | None:
    """
    Assemble a one-time <extra_instructions> block from a file path and optional prepend text.

    Reads the file at the absolute path given by ``include_extra_instructions`` and wraps it
    (together with any ``extra_instructions_prepend`` text) in an XML block that is meant to
    be prepended to the user message before the SDK call.

    This function is fully generic — it only knows about an absolute path and optional prepend
    text; no webapp-specific logic lives here. Any feature can reuse it by passing a different
    path and prepend string.

    Returns:
        The assembled block string, or None if both arguments are absent.
        If the file is missing/unreadable, the block is assembled without file contents and a
        warning is logged — the prepend text is still included so the agent retains orientation.
    """
    if not include_extra_instructions and not extra_instructions_prepend:
        return None

    parts: list[str] = []

    if extra_instructions_prepend:
        parts.append(extra_instructions_prepend)

    if include_extra_instructions:
        file_path = Path(include_extra_instructions)
        try:
            file_contents = file_path.read_text(encoding="utf-8")
            parts.append(file_contents)
        except FileNotFoundError:
            logger.warning(
                "include_extra_instructions file not found: %s — skipping file contents",
                include_extra_instructions,
            )
        except Exception as exc:
            logger.warning(
                "Failed to read include_extra_instructions file %s: %s — skipping file contents",
                include_extra_instructions,
                exc,
            )

    if not parts:
        return None

    body = "\n\n".join(parts)
    return f"<extra_instructions>\n{body}\n</extra_instructions>"


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

    # Assemble one-time extra instructions block (if requested by backend)
    extra_block = _build_extra_instructions_block(
        request.include_extra_instructions,
        request.extra_instructions_prepend,
    )
    effective_message = (
        f"{extra_block}\n\n{request.message}" if extra_block else request.message
    )

    response_content = []
    new_session_id = request.session_id

    try:
        async for chunk in sdk_manager.send_message_stream(
            message=effective_message,
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

    # Assemble one-time extra instructions block (if requested by backend)
    extra_block = _build_extra_instructions_block(
        request.include_extra_instructions,
        request.extra_instructions_prepend,
    )
    effective_message = (
        f"{extra_block}\n\n{request.message}" if extra_block else request.message
    )
    if extra_block:
        logger.info(
            "Prepending extra_instructions block (%d chars) to message for backend_session_id=%s",
            len(extra_block),
            request.backend_session_id,
        )

    async def event_stream():
        """Generate SSE events from SDK stream"""
        event_count = 0
        session_id_for_cleanup = request.session_id
        try:
            logger.info("Calling sdk_manager.send_message_stream()...")
            async for chunk in sdk_manager.send_message_stream(
                message=effective_message,
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
        Session context dict with integration_type, agent_id, bundle_id, etc.
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
        "bundle_id": None,
        "bundle_uuid": None,
        "is_publisher_install": False,
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


@router.get("/prompt/building", dependencies=[Depends(verify_auth_token)])
async def get_building_prompt():
    """
    Assemble and return the building mode system prompt.

    Returns the full assembled prompt that the building agent would receive,
    along with the individual raw parts and environment settings. Used by the
    CLI to display and diff the building context during local development.

    Response:
    {
        "building_prompt": "...",
        "building_prompt_parts": { ... },
        "prompt_files": {
            "WEBAPP_BUILDING.md": "...",
            "COMPLEX_AGENT_DESIGN.md": "..."
        },
        "settings": { ... }
    }

    ``prompt_files`` ships the contents of the companion guides in
    ``/app/core/prompts/`` (everything except ``BUILDING_AGENT.md`` itself)
    so the CLI can mirror them next to ``BUILDING_AGENT.md`` in the local
    workspace. Without this, ``./WEBAPP_BUILDING.md`` and
    ``./COMPLEX_AGENT_DESIGN.md`` references in the building prompt won't
    resolve in live-sync mode.
    """
    try:
        prompt_gen = PromptGenerator(WORKSPACE_DIR)

        # Generate assembled building mode prompt
        prompt_result = prompt_gen.generate_building_mode_prompt()
        if prompt_result and isinstance(prompt_result, dict):
            building_prompt = prompt_result.get("append", "")
        else:
            building_prompt = ""

        # Collect individual raw parts
        knowledge_topics_str = prompt_gen._get_knowledge_topics()
        if knowledge_topics_str:
            knowledge_topics = [t.strip() for t in knowledge_topics_str.split(",") if t.strip()]
        else:
            knowledge_topics = []

        building_prompt_parts = {
            "building_agent_md": prompt_gen.building_agent_prompt,
            "scripts_readme": prompt_gen._load_scripts_readme(),
            "workflow_prompt": prompt_gen._load_workflow_prompt(),
            "entrypoint_prompt": prompt_gen._load_entrypoint_prompt(),
            "refiner_prompt": prompt_gen._load_refiner_prompt(),
            "credentials_readme": prompt_gen._load_credentials_readme(),
            "knowledge_topics": knowledge_topics,
            "handover_config": prompt_gen._load_handover_prompt(),
            "plugin_instructions": None,
        }

        prompt_files: dict[str, str] = {}
        prompts_dir = Path("/app/core/prompts")
        if prompts_dir.is_dir():
            for entry in prompts_dir.iterdir():
                if not entry.is_file() or entry.suffix != ".md":
                    continue
                if entry.name == "BUILDING_AGENT.md":
                    continue
                try:
                    prompt_files[entry.name] = entry.read_text(encoding="utf-8")
                except OSError as e:
                    logger.warning("Could not read companion prompt %s: %s", entry.name, e)

        settings = {
            "agent_name": ENV_NAME,
            "template": None,
            "sdk_adapter_building": os.getenv("SDK_ADAPTER_BUILDING"),
            "model_override_building": os.getenv("MODEL_OVERRIDE_BUILDING"),
        }

        return {
            "building_prompt": building_prompt,
            "building_prompt_parts": building_prompt_parts,
            "prompt_files": prompt_files,
            "settings": settings,
        }

    except Exception as e:
        logger.error(f"Failed to assemble building prompt: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to assemble building prompt: {str(e)}"
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
            service_account_files=credentials.service_account_files,
            ssh_keys=credentials.ssh_keys,
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


@router.post("/workspace/upload", dependencies=[Depends(verify_auth_token)])
async def upload_workspace_tarball(request: Request):
    """
    Accept a gzipped tar archive and extract it to the workspace directory.

    This is the reverse of GET /workspace/download/. Used by the CLI to push
    a local workspace snapshot into the running environment.

    Request:
    - Content-Type: application/tar+gzip
    - Body: raw .tar.gz bytes

    Response:
    {
        "status": "ok",
        "message": "Workspace updated",
        "files_extracted": 42
    }

    Security:
    - Validates auth token
    - Rejects archive entries with absolute paths or path traversal (..)
    - Verifies all resolved paths remain within workspace directory

    Error Handling:
    - 400: Archive contains path traversal entries
    - 500: Extraction failed
    """
    body = await request.body()
    try:
        files_extracted = agent_env_service.extract_workspace_tarball(body)
        return {
            "status": "ok",
            "message": "Workspace updated",
            "files_extracted": files_extracted,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid archive: {str(e)}")
    except IOError as e:
        logger.error(f"Failed to extract workspace tarball: {e}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")


@router.get("/workspace/manifest", dependencies=[Depends(verify_auth_token)])
async def get_workspace_manifest():
    """
    Return a SHA-256 manifest of all files in the workspace directory.

    Used by the CLI to diff local and remote workspace state before deciding
    what to push or pull.

    Response:
    {
        "files": {
            "scripts/main.py": {"sha256": "abc...", "size": 1234, "mtime": 1712567890.0},
            "docs/WORKFLOW_PROMPT.md": {"sha256": "def...", "size": 567, "mtime": 1712567800.0}
        }
    }

    Notes:
    - Hidden files and directories (names starting with ".") are excluded
    - Symlinks are excluded
    - Directories are not included — only regular files

    Error Handling:
    - 500: Workspace not accessible or manifest generation failed
    """
    try:
        manifest = agent_env_service.get_workspace_manifest()
        return {"files": manifest}
    except IOError as e:
        logger.error(f"Failed to generate workspace manifest: {e}")
        raise HTTPException(status_code=500, detail=f"Manifest generation failed: {str(e)}")


@router.post("/files/upload", response_model=FileUploadResponse, dependencies=[Depends(verify_auth_token)])
async def upload_file_to_workspace(
    file: UploadFile = File(...),
    filename: str = Form(...),
    subfolder: str = Form(""),
) -> FileUploadResponse:
    """
    Receive file from backend and store to workspace/uploads/ directory.

    Request:
    - file: Binary file content (multipart/form-data)
    - filename: Suggested filename (will be sanitized)
    - subfolder: Optional subfolder within uploads/ (e.g., "task_TASK-1")
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

    # Determine uploads directory (with optional subfolder)
    uploads_dir = Path("/app/workspace/uploads")
    safe_subfolder = ""
    if subfolder:
        # Sanitize subfolder to prevent directory traversal
        safe_subfolder = AgentEnvService.sanitize_filename(subfolder)
        if safe_subfolder:
            uploads_dir = uploads_dir / safe_subfolder
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Handle filename conflicts
    final_filename = AgentEnvService.resolve_filename_conflict(safe_filename, uploads_dir)

    # Target path
    target_path = uploads_dir / final_filename

    # Write file
    content = await file.read()
    target_path.write_bytes(content)

    # Return relative path (what agent will see)
    relative_prefix = f"./uploads/{safe_subfolder}" if safe_subfolder else "./uploads"
    return FileUploadResponse(
        path=f"{relative_prefix}/{final_filename}",
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
    # Strip .py extension if caller included it in the URL
    if endpoint.endswith(".py"):
        endpoint = endpoint[:-3]
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


# ── Shell command execution endpoint ─────────────────────────────────────────


class _ExecRequest(_BaseModel):
    """Request body for /exec endpoint."""
    command: str
    timeout: int = 120
    # Optional extra env vars merged with os.environ before the subprocess
    # runs. Callers (e.g. the agent webhook service) use this to inject
    # WEBHOOK_PAYLOAD / WEBHOOK_HEADERS_JSON so scripts read from env vars
    # rather than string-interpolating untrusted payload bytes into the
    # command line.
    env: dict[str, str] | None = None
    # Optional text piped to the subprocess's stdin.
    stdin: str | None = None


class _ExecResponse(_BaseModel):
    """Response body for /exec endpoint."""
    exit_code: int
    stdout: str
    stderr: str


# Per-value size cap for extra env vars (100 KB). Prevents callers from
# abusing the subprocess environment to smuggle huge payloads.
_EXEC_ENV_VALUE_MAX_BYTES = 100 * 1024


@router.post("/exec", dependencies=[Depends(verify_auth_token)])
async def exec_command(request: _ExecRequest) -> _ExecResponse:
    """
    Execute a shell command inside the agent environment workspace.

    Used by the backend scheduler (script_trigger) and by agent webhook
    script triggers. The command runs in /app/workspace/ with the same
    permissions as the agent.

    Optional fields:
    - ``env``: extra env vars merged with ``os.environ.copy()`` (callers can
      still rely on PATH / HOME / etc. being present). Each value is capped
      at 100 KB.
    - ``stdin``: text piped to the subprocess's stdin.

    Returns exit_code, stdout, and stderr. Output is truncated to 10,000 chars each.
    Timeout defaults to 120 seconds (max 300 seconds).
    """
    _MAX_OUTPUT = 10_000
    timeout = min(request.timeout, 300)

    # Build subprocess env: start from current process env so PATH/HOME are
    # preserved, then overlay the caller-supplied values.
    proc_env: dict[str, str] | None = None
    if request.env is not None:
        proc_env = os.environ.copy()
        for key, value in request.env.items():
            if value is None:
                continue
            value_str = str(value)
            # Cap per-value size to avoid abuse
            if len(value_str.encode("utf-8", errors="replace")) > _EXEC_ENV_VALUE_MAX_BYTES:
                value_str = value_str.encode("utf-8", errors="replace")[:_EXEC_ENV_VALUE_MAX_BYTES].decode("utf-8", errors="replace")
            proc_env[str(key)] = value_str

    try:
        proc = await asyncio.create_subprocess_shell(
            request.command,
            stdin=asyncio.subprocess.PIPE if request.stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/app/workspace",
            env=proc_env,
        )

        stdin_bytes: bytes | None = None
        if request.stdin is not None:
            stdin_bytes = request.stdin.encode("utf-8", errors="replace")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return _ExecResponse(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
            )

        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        if len(stdout_str) > _MAX_OUTPUT:
            stdout_str = stdout_str[:_MAX_OUTPUT] + "\n[output truncated]"
        if len(stderr_str) > _MAX_OUTPUT:
            stderr_str = stderr_str[:_MAX_OUTPUT] + "\n[output truncated]"

        return _ExecResponse(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_str,
            stderr=stderr_str,
        )

    except Exception as e:
        logger.error(f"exec_command error: {e}", exc_info=True)
        return _ExecResponse(
            exit_code=-1,
            stdout="",
            stderr=f"Execution error: {str(e)}",
        )


# ── /run command streaming ────────────────────────────────────────────────────

# Module-level dict: exec_id → asyncio.subprocess.Process
# Used by /command/interrupt/{exec_id} for SIGTERM/SIGKILL routing.
_active_execs: dict[str, asyncio.subprocess.Process] = {}


@router.post("/command/stream", dependencies=[Depends(verify_auth_token)])
async def stream_command(request: CommandStreamRequest):
    """
    Stream a shell command execution via SSE.

    Called by the backend stream_command_via_agent_env method for /run:<name> commands.
    Emits a sequence of SSE events:
      1. tool event (bash invocation)
      2. tool_result_delta events (stdout/stderr chunks)
      3. done event (exit code, duration) — always emitted via try/finally

    Auth: Authorization: Bearer {AGENT_AUTH_TOKEN} (same as /chat/stream).
    """
    exec_id = request.exec_id
    command = request.command
    timeout = request.timeout
    max_output_bytes = request.max_output_bytes

    async def generate():
        import time as _time

        # Emit tool invocation event immediately
        tool_event = {
            "type": "tool",
            "tool_name": "bash",
            "content": exec_id,
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": command},
                "synthesized": True,
            },
        }
        yield f"data: {json.dumps(tool_event)}\n\n"

        proc = None
        start_time = _time.monotonic()
        exit_code = -1
        timed_out = False
        interrupted = False
        total_bytes = 0

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _active_execs[exec_id] = proc

            # Read stdout and stderr concurrently via a shared asyncio.Queue
            output_queue: asyncio.Queue = asyncio.Queue()

            async def _read_stream(stream, stream_name: str):
                while True:
                    try:
                        chunk = await stream.read(4096)
                    except Exception:
                        break
                    if not chunk:
                        break
                    await output_queue.put((stream_name, chunk.decode("utf-8", errors="replace")))
                await output_queue.put((stream_name, None))  # EOF sentinel

            read_stdout = asyncio.create_task(_read_stream(proc.stdout, "stdout"))
            read_stderr = asyncio.create_task(_read_stream(proc.stderr, "stderr"))

            # Drain the output queue, collecting SSE lines, with overall timeout
            sse_lines_to_yield: list[str] = []
            deadline = _time.monotonic() + timeout
            seen_eof = 0

            while seen_eof < 2 and not interrupted:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = await asyncio.wait_for(
                        output_queue.get(),
                        timeout=min(remaining, 1.0),
                    )
                except asyncio.TimeoutError:
                    if _time.monotonic() >= deadline:
                        timed_out = True
                    break
                stream_name, data = item
                if data is None:
                    seen_eof += 1
                    continue
                total_bytes += len(data.encode("utf-8"))
                if total_bytes > max_output_bytes:
                    trunc_event = {
                        "type": "tool_result_delta",
                        "content": f"\n[...output truncated at {max_output_bytes // 1024} KB]",
                        "metadata": {"tool_id": exec_id, "stream": "stderr"},
                    }
                    sse_lines_to_yield.append(f"data: {json.dumps(trunc_event)}\n\n")
                    interrupted = True
                    break
                chunk_event = {
                    "type": "tool_result_delta",
                    "content": data,
                    "metadata": {"tool_id": exec_id, "stream": stream_name},
                }
                sse_lines_to_yield.append(f"data: {json.dumps(chunk_event)}\n\n")

            # Yield all collected SSE lines
            for sse_line in sse_lines_to_yield:
                yield sse_line

            # Cancel reader tasks if still running
            for task in (read_stdout, read_stderr):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

            if timed_out:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass

            if interrupted:
                # Process was truncated — send interrupt
                try:
                    proc.terminate()
                    await asyncio.sleep(1)
                    if proc.returncode is None:
                        proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                interrupted_event = {"type": "interrupted", "exit_code": -1}
                yield f"data: {json.dumps(interrupted_event)}\n\n"
                return

            # Wait for process to exit (may already be done)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

            exit_code = proc.returncode if proc.returncode is not None else -1

        except Exception as exc:
            logger.error(f"command stream subprocess error: {exc}", exc_info=True)
            error_event = {
                "type": "error",
                "content": f"Failed to start command: {exc}",
                "error_type": "SubprocessError",
            }
            yield f"data: {json.dumps(error_event)}\n\n"
            return

        finally:
            _active_execs.pop(exec_id, None)
            duration_seconds = _time.monotonic() - start_time
            if not interrupted:
                done_event: dict = {
                    "type": "done",
                    "exit_code": exit_code,
                    "duration_seconds": round(duration_seconds, 2),
                }
                if timed_out:
                    done_event["timed_out"] = True
                yield f"data: {json.dumps(done_event)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.websocket("/sync/exec")
async def sync_exec_ws(websocket: WebSocket) -> None:
    """
    Internal WebSocket endpoint for Mutagen agent stdio tunneling.

    This endpoint is internal-only — reachable only from the platform backend
    over the internal Docker network. It spawns the mutagen-agent subprocess
    and pipes its stdin/stdout bidirectionally to the WebSocket connection.

    Protocol:
    1. Verify Authorization header matches AGENT_AUTH_TOKEN (same as HTTP routes).
    2. Accept connection.
    3. Read preamble frame: JSON {"remote_command": ["mutagen-agent", ...args]}.
    4. Spawn subprocess: mutagen-agent <args> with cwd=WORKSPACE_ROOT.
    5. Run concurrently:
       - Receive WS bytes → write to subprocess stdin
       - Read subprocess stdout → send as WS bytes
       - Subprocess stderr → logged (not sent to client)
    6. On WS close or process exit: kill subprocess (SIGTERM, then SIGKILL after 2s), close WS cleanly.
    """
    import json as _json

    WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/workspace")

    # ── Auth: verify AGENT_AUTH_TOKEN before accepting ───────────────────────
    if AGENT_AUTH_TOKEN:
        auth_header = websocket.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1]
        if token != AGENT_AUTH_TOKEN:
            await websocket.close(code=1008)
            return

    await websocket.accept()
    logger.info("sync/exec: WebSocket accepted")

    # ── Read preamble ────────────────────────────────────────────────────────
    try:
        preamble_raw = await asyncio.wait_for(websocket.receive_bytes(), timeout=10.0)
        preamble = _json.loads(preamble_raw)
        remote_command = preamble.get("remote_command", [])
    except asyncio.TimeoutError:
        logger.warning("sync/exec: timed out waiting for preamble")
        await websocket.close(code=1008)
        return
    except Exception as e:
        logger.error(f"sync/exec: error reading preamble: {e}")
        await websocket.close(code=1008)
        return

    # Mutagen's OpenSSH transport passes the remote command as a single
    # shell-escaped string (e.g. ".mutagen/agents/0.18.1/mutagen-agent synchronizer ...").
    # Other ssh variants may pass it pre-split. Handle both.
    import shlex as _shlex
    if isinstance(remote_command, str):
        tokens = _shlex.split(remote_command)
    elif len(remote_command) == 1 and " " in remote_command[0]:
        tokens = _shlex.split(remote_command[0])
    else:
        tokens = list(remote_command)

    if not tokens:
        logger.warning("sync/exec: empty remote_command")
        await websocket.close(code=1008)
        return

    # Mutagen references the agent by a versioned path (.mutagen/agents/<ver>/mutagen-agent).
    # We always resolve to the PATH-installed binary and ignore the caller's path prefix.
    if not os.path.basename(tokens[0]).startswith("mutagen-agent"):
        logger.warning(f"sync/exec: unexpected preamble remote_command: {remote_command}")
        await websocket.close(code=1008)
        return

    args = tokens[1:]
    logger.info(f"sync/exec: spawning mutagen-agent with args={args}, cwd={WORKSPACE_ROOT}")

    # ── Spawn subprocess ─────────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            "mutagen-agent",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_ROOT,
        )
    except FileNotFoundError:
        logger.error("sync/exec: mutagen-agent binary not found. Is it installed in the env image?")
        await websocket.close(code=1011)
        return
    except Exception as e:
        logger.error(f"sync/exec: failed to spawn mutagen-agent: {e}")
        await websocket.close(code=1011)
        return

    logger.info(f"sync/exec: mutagen-agent spawned (pid={proc.pid})")

    # ── Concurrent pump tasks ────────────────────────────────────────────────

    async def ws_to_stdin():
        """WebSocket → mutagen-agent stdin."""
        try:
            while True:
                data = await websocket.receive_bytes()
                proc.stdin.write(data)
                await proc.stdin.drain()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def stdout_to_ws():
        """mutagen-agent stdout → WebSocket."""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def log_stderr():
        """mutagen-agent stderr → env-core log."""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"mutagen-agent stderr: {line.decode(errors='replace').rstrip()}")
        except Exception:
            pass

    async def kill_proc_on_timeout(timeout: float = 2.0):
        """SIGTERM then SIGKILL with grace period."""
        try:
            proc.terminate()
            await asyncio.sleep(timeout)
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.debug(f"sync/exec: error killing mutagen-agent: {e}")

    # Run all pumps concurrently; finish when any of them exits (proc exited or WS closed).
    # Each pump must be wrapped in its own Task — asyncio.gather() returns a _GatheringFuture,
    # and asyncio.create_task requires a coroutine, not a future.
    pump_tasks = [
        asyncio.create_task(ws_to_stdin()),
        asyncio.create_task(stdout_to_ws()),
        asyncio.create_task(log_stderr()),
    ]
    proc_wait_task = asyncio.create_task(proc.wait())

    try:
        done, pending = await asyncio.wait(
            [*pump_tasks, proc_wait_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as e:
        logger.debug(f"sync/exec: pump error: {e}")
    finally:
        for task in pump_tasks:
            if not task.done():
                task.cancel()
        if not proc_wait_task.done():
            proc_wait_task.cancel()
        if proc.returncode is None:
            await kill_proc_on_timeout()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"sync/exec: session ended (pid={proc.pid}, returncode={proc.returncode})")


@router.post("/command/interrupt/{exec_id}", dependencies=[Depends(verify_auth_token)])
async def interrupt_command(exec_id: str):
    """
    Interrupt a running command by exec_id.

    Sends SIGTERM first; if the process is still alive after 2 seconds, sends SIGKILL.
    Returns {"status": "ok"} in all cases (fire-and-forget on the caller's side).

    Auth: Authorization: Bearer {AGENT_AUTH_TOKEN}.
    """
    proc = _active_execs.get(exec_id)
    if proc:
        try:
            proc.terminate()
            await asyncio.sleep(2)
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass  # Process already exited
        except Exception as exc:
            logger.warning(f"interrupt_command error for exec_id={exec_id}: {exc}")
    else:
        logger.info(f"interrupt_command: exec_id={exec_id} not found (may have already finished)")
    return {"status": "ok"}
