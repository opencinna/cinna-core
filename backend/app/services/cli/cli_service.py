"""
CLI Service.

Handles setup token lifecycle, CLI token management, workspace sync (initial tarball clone),
building context, knowledge search, remote exec streaming, and sync-runtime info for
the cinna-cli live sync model.
"""
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.core.config import settings
from app.models import Agent, AgentEnvironment
from app.models.cli.cli_setup_token import CLISetupToken, CLISetupTokenCreated
from app.models.cli.cli_token import CLIToken, CLITokenPublic
from app.services.cli.cli_auth import CLIAuthService

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). Handles naive datetimes from DB."""
    if dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Rolling expiry window for CLI tokens
CLI_TOKEN_EXPIRY_DAYS = 7
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
            from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager

            lifecycle = EnvironmentLifecycleManager()
            from app.core.db import engine

            with Session(engine) as fresh_db:
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
        from app.core.db import engine

        deadline = asyncio.get_event_loop().time() + 120
        while asyncio.get_event_loop().time() < deadline:
            with Session(engine) as fresh_db:
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
        user_id: uuid.UUID,
        request: Request,
    ) -> CLISetupTokenCreated:
        """
        Create a short-lived setup token for the given agent.

        Verifies agent ownership. Returns a CLISetupTokenCreated with the
        curl oneliner setup command.
        """
        # Verify agent ownership
        agent = db.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.owner_id != user_id:
            raise ValueError("Not allowed to create setup tokens for this agent")

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
    async def search_knowledge(
        db: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        topic: str | None = None,
    ) -> list[dict]:
        """
        Search the agent's configured knowledge sources.

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

        agent = db.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")

        workspace_id = agent.user_workspace_id

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


    # ── Live Sync ────────────────────────────────────────────────────────

    @staticmethod
    def get_sync_runtime_info() -> dict:
        """
        Return the pinned Mutagen version and agent binary hash for CLI version verification.

        The CLI calls this endpoint during `cinna setup` and `cinna sync start` to verify
        that the installed Mutagen version matches what the platform expects.
        """
        return {
            "mutagen_version": "0.18.1",
            "mutagen_agent_sha256": "",  # Populated from env image metadata when available
            "platform_api_version": "1.0",
        }

    @staticmethod
    async def stream_exec(
        environment: AgentEnvironment,
        command: str,
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

        Yields:
            SSE-framed bytes for each event
        """
        import json as _json
        from app.services.sessions.message_service import MessageService
        from app.services.environments.agent_env_connector import agent_env_connector

        exec_id = str(uuid.uuid4())
        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        # Emit exec_id as the first event so the CLI can route interrupts
        exec_id_event = _json.dumps({"type": "exec_id", "exec_id": exec_id})
        yield f"data: {exec_id_event}\n\n".encode("utf-8")

        try:
            async for event in agent_env_connector.stream_command(
                base_url=base_url,
                auth_headers=auth_headers,
                exec_id=exec_id,
                resolved_command=command,
            ):
                yield f"data: {_json.dumps(event)}\n\n".encode("utf-8")
        except Exception as e:
            error_event = _json.dumps({"type": "error", "content": f"Stream error: {e}"})
            yield f"data: {error_event}\n\n".encode("utf-8")



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
