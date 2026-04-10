"""
CLI API Routes.

Provides two routers:
- setup_router: /api/cli-setup/{token} (no auth, short URL for curl oneliner)
- router: /cli prefix under /api/v1 (user auth + CLI token auth)
"""
import uuid
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.api.deps import CLIContext, CLIContextDep, CurrentUser, SessionDep
from app.models import Message
from app.models.cli.cli_setup_token import CLISetupTokenCreate, CLISetupTokenCreated
from app.models.cli.cli_token import CLITokensPublic, CLITokenPublic
from app.services.cli.cli_service import CLIService


def _verify_cli_agent_scope(cli_ctx: CLIContext, agent_id: uuid.UUID) -> None:
    """Verify the CLI token is scoped to the requested agent."""
    if cli_ctx.agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Token is not scoped to this agent")

# ── Setup Bootstrap Router ───────────────────────────────────────────────────
# Registered directly on the FastAPI app at top level (short URL for curl oneliner)

setup_router = APIRouter(prefix="/api/cli-setup", tags=["cli"])


from pydantic import BaseModel


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
import shutil, subprocess, sys

SETUP_URL = "{setup_url}"

def main():
    cinna = shutil.which("cinna")
    if cinna:
        print("Found cinna CLI, running setup...")
        sys.exit(subprocess.call([cinna, "setup", SETUP_URL]))

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

@router.get("/agents/{agent_id}/build-context")
def get_build_context(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> StreamingResponse:
    """
    Download the Docker build context tarball for local development.

    Returns a .tar.gz containing:
    - Dockerfile, pyproject.toml, uv.lock from the environment template
    - app/core/ from app_core_base
    - A generated docker-compose.yml for local use (no server, just runtime)
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    return CLIService.get_build_context(
        db=db,
        agent=cli_ctx.agent,
        environment=cli_ctx.environment,
    )


@router.get("/agents/{agent_id}/credentials")
def get_credentials(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> Any:
    """
    Pull credentials for local development use.

    Returns credentials with decrypted values. The user accepts that
    credentials are exposed to their local machine.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    try:
        credentials = CLIService.get_credentials_for_cli(
            db=db,
            agent_id=agent_id,
            user_id=cli_ctx.user.id,
        )
        return {"credentials": credentials}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve credentials: {str(e)}",
        )


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

    Used for initial clone (cinna setup) and subsequent pulls (cinna pull).
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    if not cli_ctx.environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active environment for this agent",
        )

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


@router.post("/agents/{agent_id}/workspace", response_model=Message)
async def upload_workspace(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
    file: UploadFile = File(...),
) -> Any:
    """
    Upload local workspace to the remote environment.

    Used by cinna push to sync local changes to production.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    if not cli_ctx.environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active environment for this agent",
        )

    try:
        await CLIService.upload_workspace(
            db=db,
            agent=cli_ctx.agent,
            environment=cli_ctx.environment,
            file=file,
        )
        return Message(message="Workspace uploaded successfully")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )


@router.get("/agents/{agent_id}/workspace/manifest")
async def get_workspace_manifest(
    agent_id: uuid.UUID,
    db: SessionDep,
    cli_ctx: CLIContextDep,
) -> Any:
    """
    Get the remote workspace file manifest for diffing during push/pull.

    Returns a dict of relative paths → {sha256, size, mtime}.
    """
    _verify_cli_agent_scope(cli_ctx, agent_id)

    if not cli_ctx.environment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active environment for this agent",
        )

    try:
        return await CLIService.get_workspace_manifest(
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
