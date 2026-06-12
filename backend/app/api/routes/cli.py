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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import (
    AccountCLIContextDep,
    CLIContext,
    CLIContextDep,
    CLIContextWSDep,
    CurrentUser,
    SessionDep,
)
from app.models import Message
from app.models.agent_api.agent_api_token import ConnectAgentApiResponse
from app.models.agents.agent import AgentPublic
from app.models.cli.account_agent import AccountAgentsPublic
from app.models.cli.account_convenience import (
    AccountAgentApiEnableBody,
    AccountAgentApiRefreshBody,
    AccountAgentCreateBody,
    AccountApiProxyRequest,
    AccountConnectAgentApiBody,
    AccountConnectMcpBody,
    AccountCredentialCreateBody,
    AccountCredentialDraftResult,
    AccountCredentialShareBody,
    AccountCredentialTypesPublic,
    AccountCredentialUpdateBody,
)
from app.models.cli.cli_setup_token import CLISetupTokenCreate, CLISetupTokenCreated
from app.models.cli.cli_token import (
    CLIAccountTokensPublic,
    CLITokenPublic,
    CLITokensPublic,
)
from app.models.credentials.credential import (
    CredentialPublic,
    CredentialsPublic,
)
from app.models.mcp.mcp_provider import (
    DiscoverableAgents,
    MCPProviderConnectionResponse,
)
from app.models.users.user_workspace import UserWorkspacesPublic
from app.services.cli.account_api_proxy_policy import ApiProxyDenied
from app.services.cli.account_api_proxy_service import AccountApiProxyService
from app.services.cli.account_cli_service import (
    AccountCLIService,
    WorkspaceNotFoundError,
)
from app.services.cli.cli_service import CLIService
from app.services.cli.context_package_service import ContextPackageService

if TYPE_CHECKING:
    from app.services.agents.agent_service import CanBuildError


def _verify_cli_agent_scope(cli_ctx: CLIContext, agent_id: uuid.UUID) -> None:
    """Verify the CLI token is scoped to the requested agent."""
    if cli_ctx.agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Token is not scoped to this agent")


def _raise_can_build_http(e: "CanBuildError") -> None:
    """Map a ``CanBuildError`` reason to the right HTTP status.

    ``not_accessible`` → 404 (no existence leak), everything else → 403.
    """
    code = (
        status.HTTP_404_NOT_FOUND
        if e.reason == "not_accessible"
        else status.HTTP_403_FORBIDDEN
    )
    raise HTTPException(status_code=code, detail=e.message)


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


# ── Account Bootstrap (no auth — account setup token is the credential) ──────


@setup_router.get("/account/{token}", response_class=PlainTextResponse)
async def get_account_bootstrap_script(
    token: str,
    request: Request,
) -> str:
    """
    Serve the account bootstrap script for `curl -sL <url> | python3 -`.

    Delegates to ``cinna account setup <setup_url>`` when the CLI is installed,
    or prints install instructions otherwise.
    """
    return CLIService.render_bootstrap_script(token, request, flavor="account")


@setup_router.post("/account/{token}")
async def exchange_account_setup_token(
    token: str,
    body: ExchangeSetupTokenBody,
    request: Request,
    db: SessionDep,
) -> Any:
    """
    Exchange an account setup token for an account CLI token + bootstrap payload.

    Hit by the account `curl | python3` bootstrap script. No authentication —
    the setup token acts as the credential.
    """
    try:
        return await AccountCLIService.exchange_account_setup_token(
            db=db,
            token_str=token,
            machine_name=body.machine_name,
            machine_info=body.machine_info,
            request=request,
        )
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

    Gated by building rights (developer/admin role, not a foreign install,
    accessible) rather than bare ownership.
    """
    from app.services.agents.agent_service import CanBuildError

    try:
        return CLIService.create_setup_token(
            db=db,
            agent_id=body.agent_id,
            user=current_user,
            request=request,
        )
    except CanBuildError as e:
        _raise_can_build_http(e)
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


# ── Account CLI Routes ───────────────────────────────────────────────────────
# Account setup/management is user-JWT + developer-gated; account-scoped routes
# authenticate via the account CLI token (AccountCLIContextDep).


@router.post("/account/setup-tokens", response_model=CLISetupTokenCreated)
def create_account_setup_token(
    request: Request,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Generate an account setup token.

    Returns a curl oneliner that bootstraps the account CLI workspace. The
    token expires in 15 minutes and can only be used once. Restricted to
    ``agent-developer`` / ``admin`` (an agent-user can't even generate the link).
    """
    from app.services.users.role_service import RoleService

    try:
        RoleService.require_developer(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    return AccountCLIService.create_account_setup_token(
        db=db,
        user=current_user,
        request=request,
    )


@router.get("/account/tokens", response_model=CLIAccountTokensPublic)
def list_account_tokens(
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List the current user's account CLI tokens with synced-child counts."""
    tokens = AccountCLIService.list_account_tokens(db=db, user=current_user)
    return CLIAccountTokensPublic(data=tokens, count=len(tokens))


@router.delete("/account/tokens/{token_id}", response_model=Message)
def revoke_account_token(
    token_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Revoke an account token and cascade-revoke every child token it minted.

    On the next API call each synced agent gets 401 and Mutagen pauses. Local
    files remain intact.
    """
    try:
        count = AccountCLIService.revoke_account_token(
            db=db, token_id=token_id, user=current_user
        )
        return Message(message=f"Account token revoked; {count} session(s) disconnected")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.get("/account/agents", response_model=AccountAgentsPublic)
def list_account_agents(
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    List the agents the account token's user can access, each flagged with
    ``can_build`` / ``is_foreign_install`` / ``has_active_environment``.

    No credentials, prompts, or env internals are exposed.
    """
    items = AccountCLIService.list_accessible_agents(db=db, user=account_ctx.user)
    return AccountAgentsPublic(data=items, count=len(items))


@router.get("/account/user-workspaces", response_model=UserWorkspacesPublic)
def list_account_user_workspaces(
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    List the account user's workspaces for ``cinna account user-workspace``.

    Supplies the catalogue the CLI prints (``user-workspace list``) and validates
    the activated id against (``user-workspace --activate=<id>``). The active
    workspace is a **client-side** setting in ``.cinna/account.json`` — there is
    no server-side "active workspace"; the chosen id is sent per-create (e.g. on
    ``cinna agent create``), and the new agent's credentials inherit it.
    """
    return AccountCLIService.list_user_workspaces(db=db, user=account_ctx.user)


@router.get("/account/context-package")
def get_account_context_package(
    account_ctx: AccountCLIContextDep,
) -> StreamingResponse:
    """
    Download the orchestrator context package as a gzip tarball.

    Static platform knowledge for driving the agent network from the account
    workspace: curated platform docs, the generated REST API reference, example
    API-script patterns, and a ``context/README.md`` index the orchestrator
    ``CLAUDE.md`` points at. Contains no user-specific secrets.

    Consumed by ``cinna account setup``, which extracts it into the account
    workspace's ``context/`` tree.
    """
    return ContextPackageService.get_context_package()


class MintChildTokenBody(BaseModel):
    machine_name: str = "My Machine"
    machine_info: str | None = None


@router.post("/account/agents/{agent_id}/mint")
async def mint_child_token(
    agent_id: uuid.UUID,
    body: MintChildTokenBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Mint a normal per-agent CLI token for the target agent (provenance-stamped).

    ``can_build``-gated: 403 if not buildable, 404 if inaccessible. Returns the
    child JWT (shown once) plus the workspace-bootstrap fields the CLI needs to
    write a standard per-agent workspace.
    """
    from app.services.agents.agent_service import CanBuildError

    try:
        return await AccountCLIService.mint_child_token(
            db=db,
            user=account_ctx.user,
            account_token=account_ctx.cli_token,
            agent_id=agent_id,
            machine_name=body.machine_name,
            machine_info=body.machine_info,
            request=request,
        )
    except CanBuildError as e:
        _raise_can_build_http(e)


@router.delete("/account/tokens/children/{child_token_id}", response_model=Message)
async def revoke_account_child_token(
    child_token_id: uuid.UUID,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Revoke a child token minted by the calling account token (``cinna agent
    unsync``).

    Authenticated by the account CLI token itself (not a user JWT). The target
    must be a ``token_type="cli"`` child stamped with this account token's id;
    any other token (other users', children of other account tokens, account
    tokens, nonexistent ids) returns 404 — existence-leak discipline. The child
    gets 401 on its next API call.
    """
    try:
        await AccountCLIService.revoke_child_token(
            db=db,
            account_token=account_ctx.cli_token,
            child_token_id=child_token_id,
            request=request,
        )
        return Message(message="CLI token revoked successfully")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


# ── Account CLI Phase 3: convenience verbs + generic API escape hatch ────────
# All authenticated via the account CLI token (AccountCLIContextDep). The
# convenience verbs delegate to already-shipped services and reuse their 403/404
# mapping verbatim; the escape hatch re-dispatches into the rest of the API
# behind a single exclusion chokepoint. Phase 1's structural guard is untouched.


def _require_developer_account(account_ctx: AccountCLIContextDep) -> None:
    """Account-route developer gate (mirrors create_account_setup_token).

    The account workspace is a developer tool; the practical caller is always a
    developer, but we enforce explicitly so a demoted user is 403'd on the next
    state-changing call.
    """
    from app.services.users.role_service import RoleService

    try:
        RoleService.require_developer(account_ctx.user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/account/agents", response_model=AgentPublic)
async def account_create_agent(
    body: AccountAgentCreateBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Create an agent from the account workspace (thin client).

    Delegates to the normal ``AgentService.create_agent`` path — the backend
    applies ALL defaults (default AI credentials, default env template,
    environment creation) exactly as ``POST /api/v1/agents/`` does. Returns the
    full ``AgentPublic`` record. ``require_developer``-gated (mirrors the UI
    create route). ``env_name`` is accepted-but-noop in v1 (O1).
    """
    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.create_agent(
            db=db, user=account_ctx.user, body=body, request=request
        )
    except WorkspaceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/account/connect/agent-api", response_model=ConnectAgentApiResponse)
async def account_connect_agent_api(
    body: AccountConnectAgentApiBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Wire a consumer agent to a producer agent's REST API (one-click connect).

    Delegates to ``AgentApiTokenService.connect_agent_api``, which enforces
    producer ownership (404 no-leak) and consumer ownership (403). 400 if the
    producer's REST API is disabled. ``require_developer``-gated at the route.
    """
    from app.services.agent_api.agent_api_token_service import AgentApiTokenError

    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.connect_agent_api(
            db=db, user=account_ctx.user, body=body, request=request
        )
    except AgentApiTokenError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ── Account CLI agent-api producer management ────────────────────────────────
# Reach the producer-side enable / refresh / spec actions through the account
# token. These mirror the UI's Integrations → Agent REST API card (enable toggle,
# Refresh button, View Spec) so a local coding agent can build a producer API and
# verify the harvested spec without opening the browser. Ownership is enforced by
# the underlying ``AgentApiService`` (404 no-leak); ``enable`` is
# ``require_developer``-gated (a state change), ``refresh`` / ``spec`` are
# diagnostic reads open to any account-token holder.


@router.post("/account/agent-api/enable")
async def account_agent_api_enable(
    body: AccountAgentApiEnableBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Toggle a producer agent's REST API on/off (``cinna agent-api enable``).

    Mirrors the UI ``PUT /agents/{id}`` ``agent_api_enabled`` toggle. Ownership
    is checked up front (404 no-leak); returns the resulting agent-api status so
    the verb doubles as a verify. ``require_developer``-gated at the route.
    """
    from app.services.agent_api.agent_api_service import AgentApiError

    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.set_agent_api_enabled(
            db=db,
            user=account_ctx.user,
            agent_id=body.agent_id,
            enabled=body.enabled,
            request=request,
        )
    except AgentApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/account/agent-api/refresh")
async def account_agent_api_refresh(
    body: AccountAgentApiRefreshBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Force an on-demand spec + policy re-harvest (``cinna agent-api refresh``).

    Mirrors the producer ``POST /_refresh`` action; returns the resulting status
    (``last_error`` reflects a harvest failure — never raises on one). Ownership
    is checked up front (404 no-leak).
    """
    from app.services.agent_api.agent_api_service import AgentApiError

    try:
        return await AccountCLIService.refresh_agent_api(
            db=db,
            user=account_ctx.user,
            agent_id=body.agent_id,
            request=request,
        )
    except AgentApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/account/agent-api/spec")
async def account_agent_api_spec(
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
    agent_id: uuid.UUID,
) -> Any:
    """
    Return a producer agent's harvested OpenAPI spec (``cinna agent-api spec``).

    Mirrors the owner ``GET /openapi.json`` preview. 404 if the agent is
    inaccessible (no-leak), 400 if the API is disabled, 503 if the env is not
    running and the spec cache is cold.
    """
    from app.services.agent_api.agent_api_service import AgentApiError

    try:
        return await AccountCLIService.get_agent_api_spec(
            db=db, user=account_ctx.user, agent_id=agent_id
        )
    except AgentApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/account/connect/mcp/discoverable", response_model=DiscoverableAgents)
def account_list_discoverable_mcp(
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
    consumer_agent_id: uuid.UUID | None = None,
) -> Any:
    """
    Account-token passthrough to the MCP discoverable-agents picker (O2).

    Returns platform agents exposing an agent2agent connector the account user
    may consume, each with ``connector_id`` so the CLI can map
    ``--producer <agent>`` → ``connector_id`` before calling connect.
    """
    return AccountCLIService.list_discoverable_mcp_agents(
        db=db, user=account_ctx.user, consumer_agent_id=consumer_agent_id
    )


@router.post("/account/connect/mcp", response_model=MCPProviderConnectionResponse)
async def account_connect_mcp(
    body: AccountConnectMcpBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Wire a consumer agent to a producer agent's MCP (agent2agent) connector.

    Delegates to ``MCPProviderService.connect_to_agent``, which enforces producer
    connector ACL membership (403; missing/non-a2a connector → 404 no-leak) and
    consumer ownership (403). ``require_developer``-gated at the route.
    """
    from app.services.mcp_providers.mcp_provider_service import MCPProviderError

    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.connect_mcp(
            db=db, user=account_ctx.user, body=body, request=request
        )
    except MCPProviderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ── Account CLI credential drafting verbs ────────────────────────────────────
# The account CLI scaffolds credentials as *drafts* (no secret values — the user
# fills them in the UI) and wires them to agents. SECURITY: none of these routes
# ever reads or writes a credential's secret value (Decision 6). Listing/updating
# return metadata-only `CredentialPublic` (never `with-data`); create/update
# accept no `credential_data`. Reads are open to the account token; writes are
# `require_developer`-gated like the other convenience verbs.


def _raise_credential_value_error(e: ValueError) -> None:
    """Map a credentials-service ValueError → 404 (not found) or 400."""
    status_code = (
        status.HTTP_404_NOT_FOUND
        if "not found" in str(e).lower()
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=status_code, detail=str(e))


@router.get("/account/credentials/types", response_model=AccountCredentialTypesPublic)
def account_list_credential_types(
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Catalogue of credential types + the fields the user must fill per type.

    Lets the orchestrator pick a type and tell the user exactly which secret /
    config fields they'll need to supply after the draft is created.
    """
    return AccountCLIService.list_credential_types()


@router.get("/account/credentials", response_model=CredentialsPublic)
def account_list_credentials(
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
    user_workspace_id: str | None = None,
) -> Any:
    """
    List the account user's credentials (metadata only — no secret values).

    ``user_workspace_id`` follows the standard filter semantics: omitted = all,
    ``""`` = Default workspace, a UUID = that workspace. Each row carries a
    ``status`` (``complete`` / ``incomplete``) so the orchestrator can see which
    drafts still need the user to fill them.
    """
    try:
        return AccountCLIService.list_credentials(
            db=db, user=account_ctx.user, user_workspace_id=user_workspace_id
        )
    except WorkspaceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/account/credentials", response_model=AccountCredentialDraftResult)
async def account_create_credential(
    body: AccountCredentialCreateBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Create a *draft* credential (no secret value) in the active workspace.

    The credential is created empty (``status="incomplete"``); the response
    carries ``required_fields`` the user must fill plus a ``setup_url`` deep-link
    to the Credentials page. ``require_developer``-gated. 404 if
    ``user_workspace_id`` is not owned by the caller.
    """
    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.create_credential_draft(
            db=db, user=account_ctx.user, body=body, request=request
        )
    except WorkspaceNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.put("/account/credentials/{credential_id}", response_model=CredentialPublic)
async def account_update_credential(
    credential_id: uuid.UUID,
    body: AccountCredentialUpdateBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Update a credential's metadata only (never its secret value).

    ``require_developer``-gated. 404 if the credential doesn't exist or isn't
    owned by the caller.
    """
    _require_developer_account(account_ctx)
    try:
        return await AccountCLIService.update_credential_metadata(
            db=db,
            user=account_ctx.user,
            credential_id=credential_id,
            body=body,
            request=request,
        )
    except ValueError as e:
        _raise_credential_value_error(e)


@router.delete("/account/credentials/{credential_id}", response_model=Message)
async def account_delete_credential(
    credential_id: uuid.UUID,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
    force: bool = False,
) -> Any:
    """
    Delete a credential (blast-radius tier-gated).

    ``require_developer``-gated. 409 (with the structured deletion impact) when
    the credential is publisher-provided in a published bundle with active
    foreign installs, unless ``?force=true``. 404 if missing / not owned.
    """
    from app.services.credentials.credentials_service import CredentialInUseError

    _require_developer_account(account_ctx)
    try:
        await AccountCLIService.delete_credential(
            db=db,
            user=account_ctx.user,
            credential_id=credential_id,
            force=force,
            request=request,
        )
        return Message(message="Credential deleted successfully")
    except CredentialInUseError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.impact.model_dump(mode="json"),
        )
    except ValueError as e:
        _raise_credential_value_error(e)


@router.post(
    "/account/credentials/{credential_id}/share-with-agent",
    response_model=Message,
)
async def account_share_credential_with_agent(
    credential_id: uuid.UUID,
    body: AccountCredentialShareBody,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Any:
    """
    Attach a credential to an agent the account user owns (``share-with-agent``).

    Once the user fills the credential's secret value, its whitelisted fields
    sync to the agent's environment. ``require_developer``-gated. 404 if the
    credential or agent is missing / not owned.
    """
    _require_developer_account(account_ctx)
    try:
        await AccountCLIService.share_credential_with_agent(
            db=db,
            user=account_ctx.user,
            credential_id=credential_id,
            agent_id=body.agent_id,
            request=request,
        )
        return Message(message="Credential attached to agent successfully")
    except ValueError as e:
        _raise_credential_value_error(e)


@router.post("/account/api-proxy")
async def account_api_proxy(
    body: AccountApiProxyRequest,
    request: Request,
    db: SessionDep,
    account_ctx: AccountCLIContextDep,
) -> Response:
    """
    Generic authenticated escape hatch into (most of) the platform API.

    Re-dispatches ``{method, path, query, json_body}`` internally as the account
    token's owning user (request-scoped backend-only JWT) and returns the inner
    response status/body verbatim. A single exclusion chokepoint
    (``assert_api_proxy_allowed``) runs BEFORE dispatch: credential/user-mgmt/
    admin/CLI/MFA/auth/streaming surfaces are denied (403 ``excluded_path`` /
    ``excluded_method``; 400 ``malformed_path``). Per-account-token rate-limited
    (429); request/response size-capped (413/502). The response is a raw
    passthrough (no ``response_model``) — only the CLI consumes it.
    """
    try:
        return await AccountApiProxyService.proxy(
            db=db,
            account_token=account_ctx.cli_token,
            user=account_ctx.user,
            req=body,
            request=request,
        )
    except ApiProxyDenied as e:
        # malformed_path → 400; excluded_path / excluded_method → 403. (The
        # request model's ``Literal`` method type means a bad method is rejected
        # 422 by Pydantic before policy runs, so ``excluded_method`` is in
        # practice only reachable for streaming-route targets.)
        code = (
            status.HTTP_400_BAD_REQUEST
            if e.reason == "malformed_path"
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(status_code=code, detail=e.message)
