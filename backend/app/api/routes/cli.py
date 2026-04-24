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
import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.api.deps import CLIContext, CLIContextDep, CLIContextWSDep, CurrentUser, SessionDep
from app.models import Message
from app.models.cli.cli_setup_token import CLISetupTokenCreate, CLISetupTokenCreated
from app.models.cli.cli_token import CLITokensPublic, CLITokenPublic
from app.services.cli.cli_service import CLIService


def _verify_cli_agent_scope(cli_ctx: CLIContext, agent_id: uuid.UUID) -> None:
    """Verify the CLI token is scoped to the requested agent."""
    if cli_ctx.agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Token is not scoped to this agent")


async def _ensure_environment_running(cli_ctx: CLIContext, db: Any) -> None:
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
    from app.services.cli.cli_service import _get_platform_url
    platform_url = _get_platform_url(request)
    setup_url = f"{platform_url}/api/cli-setup/{token}"

    return f'''\
#!/usr/bin/env python3
"""Cinna CLI bootstrap script."""
import os, shutil, signal, subprocess, sys

SETUP_URL = "{setup_url}"


def _reattach_stdin_to_tty():
    """When invoked via `curl … | python3 -`, Python's stdin is the curl pipe,
    not the terminal. A child process inheriting this fd sees stdin as a closed
    pipe, not a tty — which breaks interactive UIs (Textual can't enter raw
    mode, terminal echoes mouse-tracking escapes as literal text, etc.).

    Re-open ``/dev/tty`` onto fd 0 so the spawned `cinna setup` gets the real
    terminal. Falls back silently if no tty is available (non-interactive CI).
    """
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return
    try:
        os.dup2(tty_fd, 0)
    finally:
        os.close(tty_fd)


def main():
    cinna = shutil.which("cinna")
    if cinna:
        print("Found cinna CLI, running setup...")
        _reattach_stdin_to_tty()
        # Ctrl+C is delivered to the whole foreground process group. Ignore it
        # in this wrapper so Python's default handler doesn't raise
        # KeyboardInterrupt inside wait() — cinna handles its own cleanup
        # (stops the container, etc.). Reset the handler in the child via
        # preexec_fn so cinna still receives SIGINT normally.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        proc = subprocess.Popen(
            [cinna, "setup", SETUP_URL],
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        sys.exit(proc.wait())

    print("cinna CLI is not installed.")
    print()
    print("Install it with one of:")
    print()
    if shutil.which("uv"):
        print("  uv tool install cinna-cli")
    else:
        print("  uv tool install cinna-cli    (recommended, install uv: https://docs.astral.sh/uv/)")
    print("  pip install cinna-cli")
    print()
    print("For local development from source:")
    print("  uv tool install -e /path/to/cinna-cli")
    print("  pip install -e /path/to/cinna-cli")
    print()
    print("Then re-run this command:")
    print(f"  curl -sL {{SETUP_URL}} | python3 -")
    sys.exit(1)

if __name__ == "__main__":
    main()
'''


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
    await _ensure_environment_running(cli_ctx, db)

    if not cli_ctx.environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active environment for this agent",
        )

    return StreamingResponse(
        CLIService.stream_exec(
            environment=cli_ctx.environment,
            command=body.command,
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

    The cinna-sync-ssh shim connects here. The route:
    1. Authenticates via CLIContextWSDep (CLI JWT, rolling-window refresh).
    2. Verifies agent scope.
    3. Ensures the environment is running (auto-activates if suspended).
    4. Registers with SyncActivityTracker (keeps env warm, updates timestamps).
    5. Opens a second WebSocket to env-core /sync/exec.
    6. Bidirectionally pumps bytes until either side closes (FIRST_COMPLETED).
    7. On disconnect: cancels remaining tasks, unregisters from tracker, closes both WS.

    A heartbeat coroutine runs alongside (every 30s) to keep last_sync_activity_at
    fresh and check for mid-session token revocation.
    """
    import logging
    from starlette.websockets import WebSocketState

    from app.services.cli.sync_activity_tracker import (
        sync_activity_tracker,
        SYNC_HEARTBEAT_INTERVAL_SECONDS,
    )
    from app.services.sessions.message_service import MessageService
    from app.services.environments.agent_env_connector import agent_env_connector

    logger = logging.getLogger(__name__)

    agent = cli_ctx.agent
    cli_token = cli_ctx.cli_token
    environment = cli_ctx.environment

    # ── 1. Verify agent scope ────────────────────────────────────────────────
    if agent.id != agent_id:
        await websocket.close(code=1008)
        return

    if not environment:
        await websocket.close(code=1013)
        return

    # ── 2. Ensure env is running ─────────────────────────────────────────────
    try:
        await CLIService.ensure_environment_running(environment, agent)
        db.refresh(environment)
    except (ValueError, RuntimeError) as e:
        logger.warning(f"sync-stream: environment not ready for agent {agent_id}: {e}")
        await websocket.close(code=1013)
        return

    # ── 3. Accept WS and register with tracker ──────────────────────────────
    await websocket.accept()

    connection_id = str(uuid.uuid4())
    sync_activity_tracker.register_sync_connection(
        db=db,
        environment_id=environment.id,
        token_id=cli_token.id,
        connection_id=connection_id,
    )

    # ── 4. Open env-core WS ──────────────────────────────────────────────────
    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)

    try:
        env_ws = await agent_env_connector.open_sync_websocket(base_url, auth_headers)
    except RuntimeError as e:
        logger.error(f"sync-stream: cannot reach env-core for env {environment.id}: {e}")
        sync_activity_tracker.unregister_sync_connection(db, environment.id, connection_id)
        await websocket.close(code=1013)
        return

    # ── 5. Bidirectional byte pump + heartbeat ───────────────────────────────
    async def client_to_env():
        try:
            while True:
                data = await websocket.receive_bytes()
                await env_ws.send(data)
        except Exception:
            pass

    async def env_to_client():
        try:
            while True:
                data = await env_ws.recv()
                if isinstance(data, str):
                    await websocket.send_text(data)
                else:
                    await websocket.send_bytes(data)
        except Exception:
            pass

    async def heartbeat_loop():
        try:
            while True:
                await asyncio.sleep(SYNC_HEARTBEAT_INTERVAL_SECONDS)
                # Mid-session revocation check (S8)
                db.refresh(cli_token)
                if cli_token.is_revoked:
                    logger.info(
                        f"sync-stream: token {cli_token.id} revoked mid-session, closing"
                    )
                    return
                sync_activity_tracker.heartbeat(db, environment.id)
        except asyncio.CancelledError:
            pass

    # Use FIRST_COMPLETED so a disconnect on either side tears down the other (B4).
    pump_a = asyncio.create_task(client_to_env())
    pump_b = asyncio.create_task(env_to_client())
    hb_task = asyncio.create_task(heartbeat_loop())

    try:
        await asyncio.wait(
            [pump_a, pump_b, hb_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as e:
        logger.debug(f"sync-stream pump ended: {e}")
    finally:
        # Cancel remaining tasks
        for task in [pump_a, pump_b, hb_task]:
            if not task.done():
                task.cancel()
        await asyncio.gather(pump_a, pump_b, hb_task, return_exceptions=True)

        # ── 6. Cleanup ───────────────────────────────────────────────────────
        try:
            await env_ws.close()
        except Exception:
            pass

        sync_activity_tracker.unregister_sync_connection(db, environment.id, connection_id)

        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except Exception:
            pass

        logger.info(f"sync-stream: connection closed for env {environment.id}, token {cli_token.id}")
