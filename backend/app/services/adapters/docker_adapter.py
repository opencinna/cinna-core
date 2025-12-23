import os
import asyncio
import httpx
import logging
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID
from datetime import datetime
import docker
from docker.models.containers import Container

from .base import (
    EnvironmentAdapter,
    EnvInitConfig,
    File,
    MessageRequest,
    MessageResponse,
    HealthResponse,
    CommandResult,
)

logger = logging.getLogger(__name__)


class DockerEnvironmentAdapter(EnvironmentAdapter):
    """
    Docker environment adapter using docker-compose.

    This adapter manages Docker containers via docker-compose CLI and Docker SDK.
    """

    def __init__(
        self,
        env_id: UUID,
        env_dir: Path,
        port: int,
        container_name: str | None = None,
        auth_token: str | None = None
    ):
        """
        Initialize Docker adapter.

        Args:
            env_id: Environment UUID
            env_dir: Path to environment directory (contains docker-compose.yml)
            port: Port number the agent's FastAPI server listens on inside the container
            container_name: Container name (defaults to agent-{env_id})
            auth_token: Authentication token for agent API calls
        """
        self.env_id = env_id
        self.env_dir = env_dir
        self.port = port
        self.container_name = container_name or f"agent-{env_id}"
        self.auth_token = auth_token

        # Use container name and port for network communication over agent-bridge
        self.base_url = f"http://{self.container_name}:{port}"

        # Docker client
        self.docker_client = docker.from_env()

    async def initialize(self, config: EnvInitConfig) -> bool:
        """
        Initialize environment:
        1. Verify directory structure
        2. Build Docker image
        3. Create .env file
        """
        logger.info(f"Initializing Docker environment: env_dir={self.env_dir}, env_id={self.env_id}")

        # Verify directory exists
        if not self.env_dir.exists():
            raise FileNotFoundError(f"Environment directory not found: {self.env_dir}")

        # Verify docker-compose.yml exists
        compose_file = self.env_dir / "docker-compose.yml"
        logger.debug(f"Checking for docker-compose.yml at {compose_file}")
        if not compose_file.exists():
            raise FileNotFoundError(f"docker-compose.yml not found in {self.env_dir}")

        # Build image using docker-compose
        logger.info(f"Building Docker image for environment {self.env_id}")
        await self._run_compose_command(["build"])
        logger.info(f"Docker image built successfully for environment {self.env_id}")

        return True

    async def start(self) -> bool:
        """Start environment using docker-compose up."""
        logger.info(f"Starting container {self.container_name} (env_id={self.env_id})")
        await self._run_compose_command(["up", "-d"])
        logger.info(f"Container {self.container_name} started, waiting for health check")

        # Wait for container to be healthy
        max_wait = 120  # seconds - increased for Docker health check timing
        waited = 0
        check_interval = 2

        while waited < max_wait:
            try:
                logger.debug(f"Health check attempt for {self.container_name} (waited: {waited}s/{max_wait}s)")
                health = await self.health_check()
                logger.debug(f"Health check response: status={health.status}, message={health.message}")

                if health.status == "healthy":
                    logger.info(f"Container {self.container_name} is healthy after {waited}s")
                    return True
                else:
                    logger.debug(f"Container {self.container_name} not yet healthy: {health.message}")

            except Exception as e:
                logger.debug(f"Health check exception for {self.container_name}: {type(e).__name__}: {e}")

            await asyncio.sleep(check_interval)
            waited += check_interval

        # Timeout - get final diagnostics
        logger.error(f"Container {self.container_name} health check timeout after {max_wait}s")
        try:
            final_health = await self.health_check()
            logger.error(f"Final health status: {final_health.status}, message: {final_health.message}")
        except Exception as e:
            logger.error(f"Final health check failed: {type(e).__name__}: {e}")

        raise TimeoutError(f"Container {self.container_name} did not become healthy within {max_wait}s")

    async def stop(self) -> bool:
        """Stop environment using docker-compose down."""
        await self._run_compose_command(["down"])
        return True

    async def delete(self) -> bool:
        """
        Delete environment and all associated resources.

        This removes:
        - Container
        - Volumes
        - Networks
        - Orphaned containers
        """
        try:
            logger.info(f"Deleting container {self.container_name} and all resources")
            # Use -v to remove volumes and --remove-orphans to clean up any orphaned containers
            await self._run_compose_command(["down", "-v", "--remove-orphans"])
            logger.info(f"Container {self.container_name} deleted successfully")
            return True
        except Exception as e:
            # Log error but don't fail - the container might already be gone
            logger.warning(f"docker-compose down failed for {self.container_name}: {e}")
            return True

    async def restart(self) -> bool:
        """Restart environment."""
        await self.stop()
        await self.start()
        return True

    def _get_headers(self) -> dict:
        """Get HTTP headers with auth token."""
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def health_check(self) -> HealthResponse:
        """Check container health via HTTP endpoint."""
        health_url = f"{self.base_url}/health"
        headers = self._get_headers()

        logger.debug(f"Health check: GET {health_url}, headers={list(headers.keys())}")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    health_url,
                    headers=headers,
                    timeout=5.0
                )

                logger.debug(f"Health check response: status_code={response.status_code}")

                if response.status_code == 200:
                    data = response.json()
                    logger.debug(f"Health check data: {data}")
                    return HealthResponse(
                        status="healthy",
                        uptime=data.get("uptime", 0),
                        message=data.get("message"),
                        timestamp=data.get("timestamp")
                    )
                else:
                    response_text = response.text[:200] if response.text else ""
                    logger.warning(f"Health check HTTP {response.status_code}: {response_text}")
                    return HealthResponse(
                        status="unhealthy",
                        uptime=0,
                        message=f"HTTP {response.status_code}: {response_text}",
                        timestamp=datetime.utcnow()
                    )
        except httpx.ConnectError as e:
            logger.debug(f"Health check connection error: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"Connection error: {e}",
                timestamp=datetime.utcnow()
            )
        except httpx.TimeoutException as e:
            logger.debug(f"Health check timeout: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"Timeout: {e}",
                timestamp=datetime.utcnow()
            )
        except Exception as e:
            logger.warning(f"Health check unexpected error: {type(e).__name__}: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"{type(e).__name__}: {e}",
                timestamp=datetime.utcnow()
            )

    async def get_status(self) -> str:
        """
        Get container status.

        Returns:
            "stopped" | "starting" | "running" | "error"
        """
        try:
            container = self.docker_client.containers.get(self.container_name)
            status = container.status

            if status == "running":
                # Check if actually healthy
                health = await self.health_check()
                if health.status == "healthy":
                    return "running"
                else:
                    return "starting"
            elif status == "exited":
                return "stopped"
            elif status == "created":
                return "starting"
            else:
                return "error"
        except docker.errors.NotFound:
            return "stopped"
        except Exception:
            return "error"

    async def set_prompts(self, workflow_prompt: str | None, entrypoint_prompt: str | None) -> bool:
        """Set prompts via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/prompts",
                    json={
                        "workflow_prompt": workflow_prompt,
                        "entrypoint_prompt": entrypoint_prompt
                    },
                    headers=self._get_headers(),
                    timeout=10.0
                )
                return response.status_code == 200
        except Exception:
            return False

    async def set_config(self, config: dict) -> bool:
        """Set config via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/settings",
                    json=config,
                    headers=self._get_headers(),
                    timeout=10.0
                )
                return response.status_code == 200
        except Exception:
            return False

    async def set_credentials(self, credentials: list[dict]) -> bool:
        """Write credentials to credentials directory."""
        cred_dir = self.env_dir / "app" / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Write credentials as JSON file
            import json
            cred_file = cred_dir / "credentials.json"
            with open(cred_file, 'w') as f:
                json.dump(credentials, f, indent=2)
            return True
        except Exception:
            return False

    async def upload_file(self, file: File) -> bool:
        """Upload file to container via volume."""
        try:
            # Files are written to host volume mount
            file_path = self.env_dir / "app" / "files" / file.path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(file.content, bytes):
                file_path.write_bytes(file.content)
            else:
                with open(file_path, 'wb') as f:
                    f.write(file.content.read())

            return True
        except Exception:
            return False

    async def download_file(self, path: str) -> File:
        """Download file from container via volume."""
        file_path = self.env_dir / "app" / "files" / path

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        return File(
            path=path,
            content=file_path.read_bytes(),
            metadata={"size": file_path.stat().st_size}
        )

    async def list_files(self, path: str = "/") -> list[str]:
        """List files in workspace."""
        base_path = self.env_dir / "app" / "files"
        target_path = base_path / path.lstrip("/")

        if not target_path.exists():
            return []

        files = []
        for item in target_path.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(base_path)
                files.append(str(rel_path))

        return files

    async def delete_file(self, path: str) -> bool:
        """Delete file from workspace."""
        file_path = self.env_dir / "app" / "files" / path

        try:
            if file_path.exists():
                file_path.unlink()
                return True
            return False
        except Exception:
            return False

    async def send_message(self, request: MessageRequest) -> MessageResponse:
        """Send message to agent via HTTP API."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat",
                json=request.model_dump(),
                headers=self._get_headers(),
                timeout=120.0
            )

            if response.status_code == 200:
                data = response.json()
                return MessageResponse(**data)
            else:
                raise Exception(f"Agent returned {response.status_code}: {response.text}")

    async def stream_message(self, request: MessageRequest) -> AsyncIterator[str]:
        """Stream message response from agent."""
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/stream",
                json=request.model_dump(),
                headers=self._get_headers(),
                timeout=120.0
            ) as response:
                async for chunk in response.aiter_text():
                    yield chunk

    async def execute_command(self, command: str, timeout: int = 60) -> CommandResult:
        """Execute command in container."""
        try:
            container = self.docker_client.containers.get(self.container_name)
            result = container.exec_run(
                cmd=["sh", "-c", command],
                stdout=True,
                stderr=True
            )

            return CommandResult(
                exit_code=result.exit_code,
                stdout=result.output.decode() if result.output else "",
                stderr=""
            )
        except Exception as e:
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr=str(e)
            )

    async def get_logs(self, lines: int = 100, follow: bool = False) -> list[str] | AsyncIterator[str]:
        """Get container logs."""
        try:
            container = self.docker_client.containers.get(self.container_name)

            if follow:
                # Stream logs
                async def log_stream():
                    for line in container.logs(stream=True, follow=True, tail=lines):
                        yield line.decode()
                return log_stream()
            else:
                # Get recent logs
                logs = container.logs(tail=lines).decode()
                return logs.split("\n")
        except Exception:
            return [] if not follow else iter([])

    # === Helper Methods ===

    async def _run_compose_command(self, args: list[str]) -> str:
        """
        Run docker-compose command.

        Args:
            args: Command arguments (e.g., ["up", "-d"])

        Returns:
            Command output
        """
        cmd = ["docker-compose", "-f", str(self.env_dir / "docker-compose.yml")] + args

        logger.debug(f"Running docker-compose: {' '.join(args)} in {self.env_dir}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.env_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            stderr_text = stderr.decode()
            logger.error(f"docker-compose {' '.join(args)} FAILED (exit code {process.returncode})")
            logger.error(f"stderr: {stderr_text}")
            raise Exception(f"docker-compose {' '.join(args)} failed: {stderr_text}")

        logger.debug(f"docker-compose {' '.join(args)} completed successfully")
        return stdout.decode()

    def get_container(self) -> Container | None:
        """Get Docker container object."""
        try:
            return self.docker_client.containers.get(self.container_name)
        except docker.errors.NotFound:
            return None
