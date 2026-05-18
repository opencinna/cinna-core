"""
CLI API Routes.

Provides two routers:
- setup_router: /api/cli-setup/{token} (no auth, short URL for curl oneliner)
- router: /cli prefix under /api/v1 (user auth + CLI token auth)

Live sync model (replaces tarball push/pull and local container):
- WSS /agents/{id}/sync-stream — Mutagen transport tunnel
- POST /agents/{id}/exec — remote command execution with streaming output
- GET  /agents/{id}/sync-runtime — pinned Mutagen version info for CLI setup
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import CLIContext, CLIContextDep, CLIContextWSDep, CurrentUser, SessionDep
from app.models import Message
from app.models.cli.cli_setup_token import CLISetupTokenCreate, CLISetupTokenCreated
from app.models.cli.cli_token import CLITokensPublic, CLITokenPublic
from app.services.cli.cli_service import CLIService


def _verify_cli_agent_scope(cli_ctx: CLIContext, agent_id: uuid.UUID) -> None:
    """Verify the CLI token is scoped to the requested agent."""
    if cli_ctx.agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Token is not scoped to this agent")


async def _ensure_environment_running(cli_ctx: CLIContext, db: Session) -> None:
    """
    Thin route-layer wrapper: delegates to CLIService.ensure_environment_running()
    and converts service exceptions to HTTP responses.
    """
    try:
        await CLIService.ensure_environment_running(cli_ctx.environment, cli_ctx.agent)
    except ValueError as e:
        # "No active environment" → 404, state conflicts → 409
        code = status.HTTP_404_NOT_FOUND if "No active" in str(e) else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    # Refresh so the route sees updated environment state after activation
    if cli_ctx.environment:
        db.refresh(cli_ctx.environment)


# ── Setup Bootstrap Router ───────────────────────────────────────────────────
# Registered directly on the FastAPI app at top level (short URL for curl oneliner)

setup_router = APIRouter(prefix="/api/cli-setup", tags=["cli"])


class ExchangeSetupTokenBody(BaseModel):
    machine_name: str = "My Machine"
    machine_info: str | None = None


@setup_router.get("/{token}", response_class=PlainTextResponse)
async def get_bootstrap_script(
    token: str,
    request: Request,
) -> str:
    """
    Serve the bootstrap script for `curl -sL <url> | python3 -`.

    The script checks if `cinna` is installed:
    - If yes: runs `cinna setup <setup_url>`
    - If no: prints install instructions and exits
    """
    return CLIService.render_bootstrap_script(token, request)


@setup_router.post("/{token}")
async def exchange_setup_token(
    token: str,
    body: ExchangeSetupTokenBody,
    request: Request,
    db: SessionDep,
) -> Any:
    """
    Exchange a CLI setup token for a long-lived CLI token + bootstrap payload.

    This endpoint is hit by the curl | python3 bootstrap script.
    No authentication required — the setup token acts as the credential.
    """
    try:
        payload = CLIService.exchange_setup_token(
            db=db,
            token_str=token,
            machine_name=body.machine_name,
            machine_info=body.machine_info,
            request=request,
        )
        return payload
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ── Authenticated CLI API Router ─────────────────────────────────────────────
# Registered under api_router → /api/v1/cli

router = APIRouter(prefix="/cli", tags=["cli"])


# ── Setup Token Management (user-auth) ──────────────────────────────────────

@router.post("/setup-tokens", response_model=CLISetupTokenCreated)
def create_setup_token(
    request: Request,
    db: SessionDep,
    current_user: CurrentUser,
    body: CLISetupTokenCreate,
) -> Any:
    """
    Generate a setup token for an agent.

    Returns a curl oneliner command to run locally that bootstraps the CLI.
    The token expires in 15 minutes and can only be used once.
    """
    try:
        return CLIService.create_setup_token(
            db=db,
            agent_id=body.agent_id,
            user_id=current_user.id,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ── CLI Token Management (user-auth) ────────────────────────────────────────

@router.get("/tokens", response_model=CLITokensPublic)
def list_cli_tokens(
    db: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID | None = None,
) -> Any:
    """
    List CLI tokens for the current user.

    Optionally filtered by agent_id to show tokens for a specific agent.
    """
    tokens = CLIService.list_tokens(db=db, user_id=current_user.id, agent_id=agent_id)
    return CLITokensPublic(
        data=[CLITokenPublic.model_validate(t) for t in tokens],
        count=len(tokens),
    )


@router.delete("/tokens/{token_id}", response_model=Message)
def revoke_cli_token(
    token_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Revoke (disconnect) a CLI token.

    The local cinna session will be disconnected on the next API call.
    Local files remain — only the session token is revoked.
    """
    try:
        CLIService.revoke_token(db=db, token_id=token_id, user_id=current_user.id)
        return Message(message="CLI token revoked successfully")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


# ── Agent-scoped CLI Routes (CLI token auth) ─────────────────────────────────

@router.get("/agents/{agent_id}/building-context")
async def get_building_context(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> Any:
    """
    Get the assembled building mode prompt + settings.

    Proxies to the env core's prompt generator running inside Docker.
    The env core assembles the full building prompt from workspace files,
    credentials, knowledge topics, plugins, etc.

    Returns minimal context if the environment is not running.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    try:
        return await CLIService.get_building_context(
            db=db,
            agent=cli_ctx.agent,
            environment=cli_ctx.environment,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve building context: {str(e)}",
        )


@router.get("/agents/{agent_id}/workspace")
async def get_workspace(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> StreamingResponse:
    """
    Download the remote workspace as a tarball.

    Used for initial clone during `cinna setup`. After first run, Mutagen
    takes over as the sync path. Auto-activates suspended environments.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)
    await _ensure_environment_running(cli_ctx, db)

    try:
        return await CLIService.get_workspace_tarball(
            db=db,
            agent=cli_ctx.agent,
            environment=cli_ctx.environment,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )


class KnowledgeSearchBody(BaseModel):
    query: str
    topic: str | None = None


@router.post("/agents/{agent_id}/knowledge/search")
async def search_knowledge(
    agent_id: uuid.UUID,
    body: KnowledgeSearchBody,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> Any:
    """
    Search the agent's knowledge sources.

    Used by the cinna MCP proxy to serve knowledge_query tool calls from
    local AI tools (Claude Code, Cursor, opencode).
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    results = await CLIService.search_knowledge(
        db=db,
        agent_id=agent_id,
        user_id=cli_ctx.user.id,
        query=body.query,
        topic=body.topic,
    )
    return {"results": results}


# ── Live Sync Routes (CLI token auth) ────────────────────────────────────────

@router.get("/agents/{agent_id}/sync-runtime")
async def get_sync_runtime(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> Any:
    """
    Return the pinned Mutagen version and agent binary hash.

    Called by the CLI during `cinna setup` and `cinna sync start` to verify
    that the locally installed Mutagen version matches what the platform expects.
    The CLI should fail fast with a clear install message if versions mismatch.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)
    return CLIService.get_sync_runtime_info()


class ExecBody(BaseModel):
    command: str
    # Wall-clock seconds the remote command may run before the env-core
    # kills it. None → CLIService applies its default.
    timeout: int | None = Field(default=None, ge=1, le=86400)


@router.post("/agents/{agent_id}/exec")
async def exec_command(
    agent_id: uuid.UUID,
    body: ExecBody,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> StreamingResponse:
    """
    Execute a command in the remote agent environment and stream output.

    Used by `cinna exec <command>` to run commands in the remote agent env
    instead of a local container. Streams stdout/stderr/exit-code events as
    chunked SSE from the env-core /command/stream endpoint.

    The first SSE event is always {"type": "exec_id", "exec_id": "<uuid>"} so
    the CLI can route interrupts to /command/interrupt/{exec_id}.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)
    # ``_ensure_environment_running`` raises 404 if ``cli_ctx.environment`` is
    # None, so we can rely on a non-None environment below.
    await _ensure_environment_running(cli_ctx, db)

    return StreamingResponse(
        CLIService.stream_exec(
            environment=cli_ctx.environment,
            command=body.command,
            timeout=body.timeout,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/agents/{agent_id}/sync-stream")
async def sync_stream_ws(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextWSDep,
) -> None:
    """
    WebSocket tunnel for Mutagen sync transport.

    Thin controller: verifies agent scope then hands the socket to
    ``CLIService.run_sync_tunnel`` which owns env-readiness, tracker
    registration, the bidirectional byte pump, heartbeat, and teardown.
    """
    if cli_ctx.agent.id != agent_id:
        await websocket.close(code=1008)
        return

    await CLIService.run_sync_tunnel(websocket, cli_ctx)
