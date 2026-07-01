"""
CLI Service.

Handles setup token lifecycle, CLI token management, workspace sync (initial tarball clone),
building context, knowledge search, remote exec streaming, and sync-runtime info for
the cinna-cli live sync model.
"""
import asyncio
import json as _json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, AsyncIterator

import httpx
from fastapi import Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from starlette.websockets import WebSocketState

from app.core.config import settings
from app.core.db import create_session, engine
from app.models import Agent, AgentEnvironment
from app.models.cli.cli_setup_token import CLISetupToken, CLISetupTokenCreated
from app.models.cli.cli_token import CLIToken, CLITokenPublic
from app.services.cli.cli_auth import CLI_TOKEN_EXPIRY_DAYS, CLIAuthService
from app.services.cli.sync_activity_tracker import (
    SYNC_HEARTBEAT_INTERVAL_SECONDS,
    sync_activity_tracker,
)
# Import as module so test patches of ``agent_env_connector.agent_env_connector``
# (at the connector module's source) are visible here.
from app.services.environments import agent_env_connector as _aec_module

if TYPE_CHECKING:
    # Imported lazily to avoid a module-load cycle with app.api.deps.
    from app.api.deps import CLIContext
    from app.models import User

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). Handles naive datetimes from DB."""
    if dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Short-lived setup token expiry
SETUP_TOKEN_EXPIRY_MINUTES = 15


def _get_platform_url(request: Request) -> str:
    """
    Derive the platform URL for the CLI setup command.

    Uses settings.FRONTEND_HOST if it looks like a real deployment URL,
    otherwise falls back to the backend's own base URL derived from the request.
    """
    frontend_host = settings.FRONTEND_HOST
    # In production, FRONTEND_HOST is the public URL users see
    # In local dev, it's typically localhost:5173 — we want the backend URL instead
    if "localhost" in frontend_host or "127.0.0.1" in frontend_host:
        # Derive from the request's base URL (this is the backend address)
        base = str(request.base_url).rstrip("/")
        return base
    return frontend_host.rstrip("/")


class CLIService:
    """
    Service for CLI local development operations.

    All methods are static — follow the same pattern as AccessTokenService.
    """

    # ── Environment Readiness ───────────────────────────────────────────

    @staticmethod
    async def ensure_environment_running(
        environment: AgentEnvironment | None,
        agent: Agent,
    ) -> None:
        """
        Ensure the environment is running for CLI workspace operations.

        Auto-activates suspended environments so CLI sync (push/pull/manifest)
        can proceed without the user waking the env from the web UI.

        Args:
            environment: The agent's active environment (may be None)
            agent: The agent instance

        Raises:
            ValueError: If no environment exists or env is in a non-recoverable state
            RuntimeError: If activation fails or times out
        """
        if not environment:
            raise ValueError("No active environment for this agent")

        env_status = environment.status
        if env_status == "running":
            return

        if env_status not in ("suspended", "activating"):
            raise ValueError(
                f"Environment is in '{env_status}' state and cannot be used for sync"
            )

        if env_status == "activating":
            # Another process already triggered activation — just wait
            logger.info(f"CLI: environment {environment.id} is already activating, polling...")
        else:
            logger.info(
                f"CLI auto-activating suspended environment {environment.id} "
                f"for agent {agent.id}"
            )
            from app.services.environments.environment_service import EnvironmentService

            # Project invariant: resolve the lifecycle manager via the
            # EnvironmentService singleton rather than instantiating
            # EnvironmentLifecycleManager() directly, so the configured adapter
            # (Docker in prod, the stub in tests) is used.
            lifecycle = EnvironmentService.get_lifecycle_manager()

            # ``create_session()`` (not ``Session(engine)``) so the activation
            # write participates in the rolled-back test transaction and is
            # patchable — otherwise the status change is invisible to callers
            # bound to a different session (and to tests).
            with create_session() as fresh_db:
                fresh_env = fresh_db.get(AgentEnvironment, environment.id)
                fresh_agent = fresh_db.get(Agent, agent.id)
                if not fresh_env or not fresh_agent:
                    raise RuntimeError("Environment or agent not found during activation")

                success = await lifecycle.activate_suspended_environment(
                    db_session=fresh_db,
                    environment=fresh_env,
                    agent=fresh_agent,
                    emit_events=True,
                )
                if not success:
                    raise RuntimeError("Failed to activate suspended environment")

        # Poll until running (handles both just-activated and already-activating cases)
        import asyncio

        deadline = asyncio.get_event_loop().time() + 120
        while asyncio.get_event_loop().time() < deadline:
            with create_session() as fresh_db:
                fresh_env = fresh_db.get(AgentEnvironment, environment.id)
                if not fresh_env:
                    raise RuntimeError("Environment disappeared during activation")
                if fresh_env.status == "running":
                    logger.info(f"CLI: environment {environment.id} is now running")
                    return
                if fresh_env.status == "error":
                    raise RuntimeError(
                        f"Environment entered error state during activation: "
                        f"{fresh_env.status_message}"
                    )
            await asyncio.sleep(3)

        raise RuntimeError(
            f"Environment {environment.id} activation timed out after 120 seconds"
        )

    # ── Setup Token Lifecycle ────────────────────────────────────────────

    @staticmethod
    def create_setup_token(
        db: Session,
        agent_id: uuid.UUID,
        user: "User",
        request: Request,
    ) -> CLISetupTokenCreated:
        """
        Create a short-lived setup token for the given agent.

        Gated by ``AgentService.assert_can_build`` (developer/admin role,
        not a foreign install, accessible) rather than bare ownership — a
        per-agent CLI session is a *building* context. Returns a
        ``CLISetupTokenCreated`` with the curl oneliner setup command.
        """
        from app.services.agents.agent_service import AgentService, CanBuildError

        user_id = user.id

        agent = db.get(Agent, agent_id)
        if not agent:
            # Surface as not-accessible so the route returns 404 (no leak).
            raise CanBuildError("not_accessible", "Agent not found")
        # Raises CanBuildError (route maps reason → 403/404).
        AgentService.assert_can_build(db, user, agent)

        # Find active environment (optional — we record whichever is active)
        env_stmt = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == agent_id,
            AgentEnvironment.is_active == True,  # noqa: E712
        )
        active_env = db.exec(env_stmt).first()
        environment_id = active_env.id if active_env else None

        # Generate a 32-char URL-safe random token
        token_value = secrets.token_urlsafe(24)  # 24 bytes → ~32 chars URL-safe

        expires_at = datetime.now(UTC) + timedelta(minutes=SETUP_TOKEN_EXPIRY_MINUTES)

        token = CLISetupToken(
            token=token_value,
            agent_id=agent_id,
            environment_id=environment_id,
            owner_id=user_id,
            expires_at=expires_at,
        )
        db.add(token)
        db.commit()
        db.refresh(token)

        platform_url = _get_platform_url(request)
        setup_command = f"curl -sL {platform_url}/api/cli-setup/{token_value} | python3 -"

        return CLISetupTokenCreated(
            id=token.id,
            token=token_value,
            agent_id=token.agent_id,
            environment_id=token.environment_id,
            expires_at=token.expires_at,
            created_at=token.created_at,
            setup_command=setup_command,
        )

    @staticmethod
    def cleanup_expired_setup_tokens(db: Session) -> int:
        """
        Delete expired setup tokens.

        Removes:
        - Used tokens older than 24 hours
        - Expired (unused) tokens
        """
        cutoff_24h = datetime.now(UTC) - timedelta(hours=24)
        cutoff_now = datetime.now(UTC)

        # Find tokens to delete
        stmt = select(CLISetupToken).where(
            (
                (CLISetupToken.is_used == True) &  # noqa: E712
                (CLISetupToken.expires_at < cutoff_24h)
            ) | (
                (CLISetupToken.is_used == False) &  # noqa: E712
                (CLISetupToken.expires_at < cutoff_now)
            )
        )
        tokens = db.exec(stmt).all()
        count = len(tokens)
        for token in tokens:
            db.delete(token)
        db.commit()
        logger.info(f"Cleaned up {count} expired CLI setup tokens")
        return count

    # ── CLI Token Lifecycle ──────────────────────────────────────────────

    @staticmethod
    def list_tokens(
        db: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID | None = None,
    ) -> list[CLIToken]:
        """List active (non-revoked, non-expired) CLI tokens for a user."""
        now = datetime.now(UTC)
        stmt = select(CLIToken).where(
            CLIToken.owner_id == user_id,
            CLIToken.is_revoked == False,  # noqa: E712
            CLIToken.expires_at > now,
        )
        if agent_id is not None:
            stmt = stmt.where(CLIToken.agent_id == agent_id)
        stmt = stmt.order_by(CLIToken.created_at.desc())
        return list(db.exec(stmt).all())

    @staticmethod
    def revoke_token(
        db: Session,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> CLIToken:
        """
        Revoke a CLI token by ID.

        Raises ValueError if not found or caller doesn't own it.
        """
        token = db.get(CLIToken, token_id)
        if not token:
            raise ValueError("CLI token not found")
        if token.owner_id != user_id:
            raise ValueError("Not allowed to revoke this token")
        token.is_revoked = True
        db.add(token)
        db.commit()
        db.refresh(token)
        return token

    @staticmethod
    def exchange_setup_token(
        db: Session,
        token_str: str,
        machine_name: str,
        machine_info: str | None,
        request: Request,
    ) -> dict:
        """
        Exchange a setup token for a CLI token + bootstrap payload.

        Validates the setup token (not used, not expired), creates a CLIToken,
        marks the setup token as used, and returns the bootstrap payload.
        """
        # Look up setup token
        stmt = select(CLISetupToken).where(CLISetupToken.token == token_str)
        setup_token = db.exec(stmt).first()
        if not setup_token:
            raise ValueError("Invalid setup token")
        # Explicitly reject account setup tokens on the per-agent exchange path
        # (defense-in-depth — an account token's agent_id is NULL and would
        # otherwise fail only incidentally on the agent lookup below).
        if setup_token.kind != "agent":
            raise ValueError("Not a per-agent setup token")

        now = datetime.now(UTC)
        if setup_token.is_used:
            raise ValueError("Setup token has already been used")
        if _ensure_utc(setup_token.expires_at) < now:
            raise ValueError("Setup token has expired")

        # Load agent
        agent = db.get(Agent, setup_token.agent_id)
        if not agent:
            raise ValueError("Agent not found")

        # Load environment
        environment: AgentEnvironment | None = None
        if setup_token.environment_id:
            environment = db.get(AgentEnvironment, setup_token.environment_id)

        # Create the CLI token
        cli_token_id = uuid.uuid4()
        cli_expires_at = now + timedelta(days=CLI_TOKEN_EXPIRY_DAYS)

        jwt_value = CLIAuthService.create_cli_jwt(
            cli_token_id=cli_token_id,
            agent_id=agent.id,
            owner_id=setup_token.owner_id,
            expires_at=cli_expires_at,
        )

        token_hash = CLIAuthService.hash_token(jwt_value)
        prefix = jwt_value[:12]

        cli_token = CLIToken(
            id=cli_token_id,
            agent_id=agent.id,
            owner_id=setup_token.owner_id,
            name=machine_name,
            token_hash=token_hash,
            prefix=prefix,
            machine_info=machine_info,
            expires_at=cli_expires_at,
        )
        db.add(cli_token)

        # Mark setup token as used
        setup_token.is_used = True
        db.add(setup_token)

        db.commit()

        platform_url = _get_platform_url(request)
        # frontend_url is the user-facing web UI — always ``settings.FRONTEND_HOST``,
        # which is distinct from ``platform_url`` (the API base) in local dev
        # (FRONTEND_HOST=http://localhost:5173 vs platform_url=http://localhost:8000)
        # and identical in production.
        frontend_url = settings.FRONTEND_HOST.rstrip("/")

        return {
            "cli_token": jwt_value,
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "environment_id": str(environment.id) if environment else None,
                "template": environment.env_name if environment else None,
            },
            "platform_url": platform_url,
            "frontend_url": frontend_url,
            # Credentials and knowledge_sources are fetched separately by the CLI
            "credentials": [],
            "knowledge_sources": [],
        }

    # ── Workspace Sync ───────────────────────────────────────────────────

    @staticmethod
    async def get_workspace_tarball(
        db: Session,
        agent: Agent,
        environment: AgentEnvironment,
    ) -> StreamingResponse:
        """
        Download the workspace from the remote environment as a tarball.

        Proxies the request to the env core HTTP API running inside Docker.
        """
        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{base_url}/workspace/download/.",
                    headers=auth_headers,
                )
                response.raise_for_status()
                content = response.content
        except httpx.HTTPStatusError as e:
            raise ValueError(f"Failed to download workspace from environment: {e.response.status_code}")
        except httpx.RequestError as e:
            raise ValueError(f"Cannot connect to environment: {e}")

        async def content_iter():
            yield content

        return StreamingResponse(
            content_iter(),
            media_type="application/tar+gzip",
            headers={"Content-Disposition": 'attachment; filename="workspace.tar.gz"'},
        )

    # ── Building Context ─────────────────────────────────────────────────

    @staticmethod
    async def get_building_context(
        db: Session,
        agent: Agent,
        environment: AgentEnvironment | None,
    ) -> dict:
        """
        Get the assembled building mode prompt + settings from the env core.

        Proxies to the env core's prompt generation endpoint. The env core
        assembles the full building prompt from workspace files, credentials,
        knowledge topics, plugins, etc.

        If no environment is available or the environment is not running,
        returns a minimal context with agent settings only.
        """
        if not environment:
            logger.warning(f"No environment for agent {agent.id} — returning minimal building context")
            return _minimal_building_context(agent)

        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{base_url}/prompt/building",
                    headers=auth_headers,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Env core returned error for building context: {e.response.status_code}. "
                "Returning minimal context."
            )
            return _minimal_building_context(agent, environment)
        except httpx.RequestError as e:
            logger.warning(
                f"Cannot connect to environment for building context: {e}. "
                "Returning minimal context."
            )
            return _minimal_building_context(agent, environment)

    # ── Knowledge Search ─────────────────────────────────────────────────

    @staticmethod
    async def search_user_knowledge(
        db: Session,
        *,
        user_id: uuid.UUID,
        query: str,
        topic: str | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> list[dict]:
        """
        Search a user's accessible knowledge sources.

        User-scoped core of the knowledge search infrastructure, independent of
        any agent. When ``workspace_id`` is ``None`` there is no workspace
        filter (all the user's own connected sources + all public sources).

        Proxies to the existing knowledge search infrastructure.
        """
        from app.services.knowledge.embedding_service import (
            DEFAULT_EMBEDDING_MODEL,
            generate_query_embedding,
        )
        from app.services.knowledge.vector_search_service import (
            VectorSearchError,
            get_accessible_source_ids,
            search_article_chunks,
        )

        try:
            query_embedding, _ = generate_query_embedding(
                query=query,
                model=DEFAULT_EMBEDDING_MODEL,
            )

            source_ids = get_accessible_source_ids(
                session=db,
                user_id=user_id,
                workspace_id=workspace_id,
            )

            if not source_ids:
                return []

            chunk_results = search_article_chunks(
                session=db,
                query_embedding=query_embedding,
                source_ids=source_ids,
                embedding_model=DEFAULT_EMBEDDING_MODEL,
                limit=10,
            )

            results = []
            for chunk in chunk_results:
                results.append({
                    "content": chunk.content if hasattr(chunk, "content") else str(chunk),
                    "source": chunk.source_name if hasattr(chunk, "source_name") else "knowledge",
                    "similarity": float(chunk.similarity) if hasattr(chunk, "similarity") else 0.0,
                })
            return results

        except VectorSearchError as e:
            logger.error(f"Knowledge search error: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error during knowledge search: {e}", exc_info=True)
            return []

    @staticmethod
    async def search_knowledge(
        db: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        topic: str | None = None,
    ) -> list[dict]:
        """
        Search the agent's configured knowledge sources.

        Resolves the workspace from the agent and delegates to
        :meth:`search_user_knowledge`.
        """
        agent = db.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")

        return await CLIService.search_user_knowledge(
            db,
            user_id=user_id,
            query=query,
            topic=topic,
            workspace_id=agent.user_workspace_id,
        )


    # ── Bootstrap Script ─────────────────────────────────────────────────

    @staticmethod
    def render_bootstrap_script(
        token: str, request: Request, flavor: str = "agent"
    ) -> str:
        """
        Render the Python bootstrap script served by the bootstrap GET routes.

        The generated script is piped into ``python3 -`` via the curl one-liner;
        it delegates to an installed ``cinna`` CLI when present or prints install
        instructions otherwise. Keeping the generator in the service layer keeps
        the route a thin controller and avoids the route importing private
        helpers from the service module.

        Args:
            token: The setup token embedded in the URL.
            request: The incoming request (used to derive the platform URL).
            flavor: ``"agent"`` → ``cinna setup <url>`` (per-agent, default);
                ``"account"`` → ``cinna account setup <url>`` against the
                ``/api/cli-setup/account/{token}`` endpoint.
        """
        platform_url = _get_platform_url(request)
        if flavor == "account":
            setup_url = f"{platform_url}/api/cli-setup/account/{token}"
            cinna_args = '[cinna, "account", "setup", SETUP_URL]'
        else:
            setup_url = f"{platform_url}/api/cli-setup/{token}"
            cinna_args = '[cinna, "setup", SETUP_URL]'

        return f'''\
#!/usr/bin/env python3
"""Cinna CLI bootstrap script."""
import os, shutil, signal, subprocess, sys

SETUP_URL = "{setup_url}"
MINIMUM_CLI_VERSION = "{settings.MINIMUM_CLI_VERSION}"


def _parse_version(text):
    """Extract a numeric version tuple from arbitrary version text.

    Finds the first whitespace token that starts with a digit (e.g. the
    "0.2.3" in `cinna --version`'s "cinna, version 0.2.3") and parses its
    leading dotted-numeric components, stopping at the first non-numeric part
    so pre-release suffixes (e.g. "0.2.3rc1") degrade to (0, 2, 3). Returns
    None when no version-looking token is found.
    """
    token = ""
    for raw in (text or "").replace(",", " ").split():
        if raw and raw[0].isdigit():
            token = raw
            break
    if not token:
        return None
    nums = []
    for part in token.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            break
        nums.append(int(digits))
    return tuple(nums) or None


def _installed_cli_version(cinna):
    """Return the installed cinna CLI version tuple, or None if undeterminable."""
    try:
        result = subprocess.run(
            [cinna, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    return _parse_version((result.stdout or "") + " " + (result.stderr or ""))


def _print_upgrade_instructions(installed):
    print("Your installed cinna CLI is too old for this platform's setup flow.")
    print()
    if installed is not None:
        print("  installed: " + ".".join(str(p) for p in installed))
    print("  required:  " + MINIMUM_CLI_VERSION + " or newer")
    print()
    print("Update it with one of:")
    if shutil.which("uv"):
        print("  uv tool upgrade cinna-cli")
    else:
        print("  uv tool upgrade cinna-cli    (recommended, install uv: https://docs.astral.sh/uv/)")
    print("  pip install --upgrade cinna-cli")
    print()
    print("Then re-run this command:")
    print("  curl -sL " + SETUP_URL + " | python3 -")


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
        installed = _installed_cli_version(cinna)
        required = _parse_version(MINIMUM_CLI_VERSION)
        # Only block when we could actually determine an older version. A version
        # we can't parse (None) is left to proceed — never falsely block a CLI
        # whose `--version` output we don't recognize.
        if installed is not None and required is not None and installed < required:
            _print_upgrade_instructions(installed)
            sys.exit(1)
        print("Found cinna CLI, running setup...")
        _reattach_stdin_to_tty()
        # Ctrl+C is delivered to the whole foreground process group. Ignore it
        # in this wrapper so Python's default handler doesn't raise
        # KeyboardInterrupt inside wait() — cinna handles its own cleanup
        # (stops the container, etc.). Reset the handler in the child via
        # preexec_fn so cinna still receives SIGINT normally.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        proc = subprocess.Popen(
            {cinna_args},
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

    # ── Live Sync ────────────────────────────────────────────────────────

    @staticmethod
    def get_sync_runtime_info() -> dict:
        """
        Return the pinned Mutagen version and agent binary hash for CLI version verification.

        The CLI calls this endpoint during `cinna setup` and `cinna sync start` to verify
        that the installed Mutagen version matches what the platform expects. The
        version strings live in ``settings`` so they stay in lockstep with the
        ``MUTAGEN_VERSION`` build arg in the env-template Dockerfiles.
        """
        return {
            "mutagen_version": settings.MUTAGEN_VERSION,
            "mutagen_agent_sha256": "",  # Populated from env image metadata when available
            "platform_api_version": settings.PLATFORM_API_VERSION,
        }

    # Default cap for `cinna exec` commands — generous enough for long
    # backfills / training runs while still bounding orphaned subprocesses.
    DEFAULT_CLI_EXEC_TIMEOUT_SECONDS = 1800

    @staticmethod
    async def stream_exec(
        environment: AgentEnvironment,
        command: str,
        timeout: int | None = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream command execution output from the env-core /command/stream endpoint.

        Routes through AgentEnvConnector.stream_command() — the same helper used
        by in-session /run:* commands. The first SSE event emits the exec_id so
        the CLI can later interrupt the command via /command/interrupt/{exec_id}.

        Yields SSE-framed bytes (data: ...\\n\\n) for each event received from the
        env-core, forwarding them verbatim to the CLI's stdout/stderr in real time.

        Args:
            environment: The agent environment to execute the command in
            command: Shell command string to execute
            timeout: Max wall-clock seconds the remote command may run; falls
                back to ``DEFAULT_CLI_EXEC_TIMEOUT_SECONDS`` when omitted.

        Yields:
            SSE-framed bytes for each event
        """
        # MessageService pulls in heavy imports (agent_service, session_service,
        # …). Keep this lazy to avoid a startup-time cycle through the sessions
        # domain just because cli_service is imported.
        from app.services.sessions.message_service import MessageService

        effective_timeout = (
            timeout if timeout is not None else CLIService.DEFAULT_CLI_EXEC_TIMEOUT_SECONDS
        )

        exec_id = str(uuid.uuid4())
        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        logger.info(
            f"cli stream_exec start: env={environment.id} exec_id={exec_id} "
            f"timeout={effective_timeout}s command={command!r}"
        )

        # Emit exec_id as the first event so the CLI can route interrupts
        exec_id_event = _json.dumps({"type": "exec_id", "exec_id": exec_id})
        yield f"data: {exec_id_event}\n\n".encode("utf-8")

        final_event_type: str | None = None
        final_exit_code: int | None = None
        try:
            async for event in _aec_module.agent_env_connector.stream_command(
                base_url=base_url,
                auth_headers=auth_headers,
                exec_id=exec_id,
                resolved_command=command,
                timeout=effective_timeout,
            ):
                etype = event.get("type")
                if etype in ("done", "interrupted", "error"):
                    final_event_type = etype
                    final_exit_code = event.get("exit_code")
                yield f"data: {_json.dumps(event)}\n\n".encode("utf-8")
        except Exception as e:
            logger.exception(
                f"cli stream_exec error: env={environment.id} exec_id={exec_id}: {e}"
            )
            error_event = _json.dumps({"type": "error", "content": f"Stream error: {e}"})
            yield f"data: {error_event}\n\n".encode("utf-8")
            final_event_type = "error"
        finally:
            logger.info(
                f"cli stream_exec end: env={environment.id} exec_id={exec_id} "
                f"final={final_event_type} exit_code={final_exit_code}"
            )

    @staticmethod
    async def run_sync_tunnel(
        websocket: WebSocket,
        cli_ctx: "CLIContext",
    ) -> None:
        """
        End-to-end lifecycle of a cinna-cli live-sync WebSocket tunnel.

        Called by the ``/cli/agents/{agent_id}/sync-stream`` route after it has
        performed the agent-scope check. Responsibilities:

        1. Ensure the agent environment is running (auto-activates if suspended).
        2. Accept the client WebSocket and register with ``SyncActivityTracker``.
        3. Open a second WebSocket to env-core ``/sync/exec``.
        4. Pump bytes bidirectionally plus a heartbeat that keeps
           ``last_sync_activity_at`` fresh and aborts on mid-session token
           revocation. Heartbeat uses its own fresh ``Session(engine)`` per tick
           rather than holding the request-scoped dep session for the whole WS
           lifetime.
        5. On teardown (either side disconnects or token revoked): cancel
           remaining tasks, close env-core WS, unregister from tracker, close
           the client WS.
        """
        agent = cli_ctx.agent
        cli_token = cli_ctx.cli_token
        environment = cli_ctx.environment

        if not environment:
            await websocket.close(code=1013)
            return

        # 1. Ensure env is running
        try:
            await CLIService.ensure_environment_running(environment, agent)
        except (ValueError, RuntimeError) as e:
            logger.warning(f"sync-stream: environment not ready for agent {agent.id}: {e}")
            await websocket.close(code=1013)
            return

        # 2. Accept WS + register with tracker
        await websocket.accept()
        connection_id = str(uuid.uuid4())
        sync_activity_tracker.register_sync_connection(
            environment_id=environment.id,
            token_id=cli_token.id,
            connection_id=connection_id,
        )

        # 3. Open env-core WS
        #    MessageService is imported lazily — see stream_exec note.
        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        try:
            env_ws = await _aec_module.agent_env_connector.open_sync_websocket(base_url, auth_headers)
        except RuntimeError as e:
            logger.error(f"sync-stream: cannot reach env-core for env {environment.id}: {e}")
            sync_activity_tracker.unregister_sync_connection(environment.id, connection_id)
            await websocket.close(code=1013)
            return

        # 4. Bidirectional byte pump + heartbeat
        async def client_to_env() -> None:
            try:
                while True:
                    data = await websocket.receive_bytes()
                    await env_ws.send(data)
            except Exception as e:
                logger.debug(f"sync-stream client→env pump ended: {e}")

        async def env_to_client() -> None:
            try:
                while True:
                    data = await env_ws.recv()
                    if isinstance(data, str):
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)
            except Exception as e:
                logger.debug(f"sync-stream env→client pump ended: {e}")

        async def heartbeat_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(SYNC_HEARTBEAT_INTERVAL_SECONDS)
                    # Check mid-session token revocation with a fresh session
                    # rather than the dep-owned one — we don't want to hold
                    # that session open for the whole WS lifetime.
                    with Session(engine) as hb_db:
                        current = hb_db.get(CLIToken, cli_token.id)
                        if current is None or current.is_revoked:
                            logger.info(
                                f"sync-stream: token {cli_token.id} revoked mid-session, closing"
                            )
                            return
                    sync_activity_tracker.heartbeat(environment.id)
            except asyncio.CancelledError:
                pass

        pump_a = asyncio.create_task(client_to_env())
        pump_b = asyncio.create_task(env_to_client())
        hb_task = asyncio.create_task(heartbeat_loop())

        try:
            # FIRST_COMPLETED so a disconnect on either side tears down the other.
            await asyncio.wait(
                [pump_a, pump_b, hb_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception as e:
            logger.debug(f"sync-stream pump ended: {e}")
        finally:
            for task in (pump_a, pump_b, hb_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pump_a, pump_b, hb_task, return_exceptions=True)

            try:
                await env_ws.close()
            except Exception:
                pass

            sync_activity_tracker.unregister_sync_connection(environment.id, connection_id)

            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.close()
            except Exception:
                pass

            logger.info(
                f"sync-stream: connection closed for env {environment.id}, token {cli_token.id}"
            )



def _minimal_building_context(
    agent: Agent,
    environment: AgentEnvironment | None = None,
) -> dict:
    """Return a minimal building context when the env core is not available."""
    return {
        "building_prompt": f"You are a building agent for '{agent.name}'. Configure and develop this agent.",
        "building_prompt_parts": {},
        "prompt_files": {},
        "settings": {
            "agent_name": agent.name,
            "template": environment.env_name if environment else None,
            "sdk_adapter_building": environment.agent_sdk_building if environment else None,
            "sdk_adapter_conversation": environment.agent_sdk_conversation if environment else None,
            "model_override_building": environment.model_override_building if environment else None,
        },
    }
