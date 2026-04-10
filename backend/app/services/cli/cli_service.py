"""
CLI Service.

Handles setup token lifecycle, CLI token management, build context assembly,
workspace sync, credentials, building context, and knowledge search for local development.
"""
import io
import logging
import os
import secrets
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.core.config import settings
from app.models import Agent, AgentEnvironment
from app.models.cli.cli_setup_token import CLISetupToken, CLISetupTokenCreated
from app.models.cli.cli_token import CLIToken, CLITokenCreated, CLITokenPublic
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

# Base directory for environment templates
_TEMPLATES_BASE = Path(__file__).parent.parent.parent / "env-templates"


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

        return {
            "cli_token": jwt_value,
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "environment_id": str(environment.id) if environment else None,
                "template": environment.env_name if environment else None,
            },
            "platform_url": platform_url,
            # Credentials and knowledge_sources are fetched separately by the CLI
            "credentials": [],
            "knowledge_sources": [],
        }

    # ── Build Context ────────────────────────────────────────────────────

    @staticmethod
    def get_build_context(
        db: Session,
        agent: Agent,
        environment: AgentEnvironment | None,
    ) -> StreamingResponse:
        """
        Assemble and stream the Docker build context tarball for local development.

        Includes:
        - Dockerfile, pyproject.toml, uv.lock from the environment template
        - app/core/ from app_core_base template
        - A generated docker-compose.yml for local use
        """
        template_name = environment.env_name if environment else "general-env"
        template_dir = _TEMPLATES_BASE / template_name

        if not template_dir.exists():
            logger.warning(f"Template dir not found: {template_dir}, falling back to general-env")
            template_dir = _TEMPLATES_BASE / "general-env"

        app_core_dir = _TEMPLATES_BASE / "app_core_base"

        agent_name = agent.name.lower().replace(" ", "-")

        # Generate local docker-compose.yml content
        compose_content = f"""services:
  agent-dev:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: agent-dev-{agent_name}
    volumes:
      - ./workspace:/app/workspace
    working_dir: /app/workspace
    stdin_open: true
    tty: true
    restart: unless-stopped
"""

        def generate_tarball():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                # Add files from environment template
                for filename in ["Dockerfile", "pyproject.toml", "uv.lock"]:
                    filepath = template_dir / filename
                    if filepath.exists():
                        tar.add(str(filepath), arcname=filename)
                    else:
                        logger.debug(f"Template file not found, skipping: {filepath}")

                # Add app/core from app_core_base
                app_core_path = app_core_dir / "app" / "core"
                if app_core_path.exists():
                    tar.add(str(app_core_path), arcname="app/core")
                else:
                    # Try the root of app_core_base
                    if app_core_dir.exists():
                        tar.add(str(app_core_dir), arcname="app")

                # Add generated docker-compose.yml
                compose_bytes = compose_content.encode("utf-8")
                info = tarfile.TarInfo(name="docker-compose.yml")
                info.size = len(compose_bytes)
                tar.addfile(info, io.BytesIO(compose_bytes))

            buf.seek(0)
            yield from buf

        return StreamingResponse(
            generate_tarball(),
            media_type="application/tar+gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{agent_name}-build-context.tar.gz"'
            },
        )

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

    @staticmethod
    async def upload_workspace(
        db: Session,
        agent: Agent,
        environment: AgentEnvironment,
        file: UploadFile,
    ) -> None:
        """
        Upload workspace tarball to the remote environment.

        Proxies the upload to the env core HTTP API.
        """
        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        content = await file.read()

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{base_url}/workspace/upload",
                    content=content,
                    headers={
                        **auth_headers,
                        "Content-Type": "application/tar+gzip",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ValueError(f"Failed to upload workspace to environment: {e.response.status_code}")
        except httpx.RequestError as e:
            raise ValueError(f"Cannot connect to environment: {e}")

        logger.info("Syncing agent prompts after CLI push")
        try:
            from app.services.environments.environment_service import EnvironmentService

            await EnvironmentService.sync_agent_prompts_from_environment(db, environment, agent)
        except Exception as e:
            logger.warning("Failed to sync agent prompts after CLI push: %s", e)

    @staticmethod
    async def get_workspace_manifest(
        db: Session,
        agent: Agent,
        environment: AgentEnvironment,
    ) -> dict:
        """
        Get the workspace file manifest from the remote environment.

        Returns a dict of relative paths → {sha256, size, mtime}.
        """
        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{base_url}/workspace/manifest",
                    headers=auth_headers,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            raise ValueError(f"Failed to get workspace manifest from environment: {e.response.status_code}")
        except httpx.RequestError as e:
            raise ValueError(f"Cannot connect to environment: {e}")

    # ── Credentials ──────────────────────────────────────────────────────

    @staticmethod
    def get_credentials_for_cli(
        db: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[dict]:
        """
        Get agent credentials with decrypted values for local CLI use.

        Uses the same CredentialsService as environment sync. Returns a list of
        credential dicts including name, type, and full credential_data.
        """
        from app.services.credentials.credentials_service import CredentialsService

        result = CredentialsService.prepare_credentials_for_environment(db, agent_id)
        return result.get("credentials_json", [])

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


def _minimal_building_context(
    agent: Agent,
    environment: AgentEnvironment | None = None,
) -> dict:
    """Return a minimal building context when the env core is not available."""
    return {
        "building_prompt": f"You are a building agent for '{agent.name}'. Configure and develop this agent.",
        "building_prompt_parts": {},
        "settings": {
            "agent_name": agent.name,
            "template": environment.env_name if environment else None,
            "sdk_adapter_building": environment.agent_sdk_building if environment else None,
            "sdk_adapter_conversation": environment.agent_sdk_conversation if environment else None,
            "model_override_building": environment.model_override_building if environment else None,
        },
    }
