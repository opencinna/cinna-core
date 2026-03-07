import os
import asyncio
import httpx
import logging
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID
from datetime import UTC, datetime
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
        """
        Start environment using docker-compose up.

        This only starts the container and waits for health check.
        Package installation and other setup should be handled by the lifecycle manager.
        """
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
        """Stop environment using docker-compose stop."""
        await self._run_compose_command(["stop"])
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

    async def rebuild(
        self,
        template_dir: Path,
        template_core_dir: Path,
        rebuild_overwrite_files: list[str],
        was_running: bool
    ) -> bool:
        """
        Rebuild environment with updated core files and knowledge base.

        Args:
            template_dir: Path to template root directory
            template_core_dir: Path to template's core directory
            rebuild_overwrite_files: List of template root files to overwrite in instance
            was_running: Whether container was running before rebuild

        Returns:
            True if rebuild successful

        Process:
        1. Container should already be stopped
        2. Remove old container (docker-compose down)
        3. Overwrite infrastructure files from template (Dockerfile, pyproject.toml, etc.)
        4. Update core files from template
        5. Update knowledge files from template (add/update only, preserve user-created files)
        6. Rebuild Docker image (includes new core files)
        7. Start container if it was running before (creates new container from new image)
        """
        logger.info(f"Rebuilding environment {self.env_id}")

        # Verify container is stopped
        status = await self.get_status()
        if status == "running":
            raise RuntimeError("Container must be stopped before rebuilding")

        # Remove old container so new one will be created from new image
        # Use 'down' without -v to keep volumes (workspace data)
        logger.info(f"Removing old container {self.container_name}")
        await self._run_compose_command(["down"])
        logger.debug(f"Old container removed")

        # Overwrite infrastructure files from template
        import shutil
        logger.info(f"Overwriting infrastructure files from template: {rebuild_overwrite_files}")
        for filename in rebuild_overwrite_files:
            src_file = template_dir / filename
            dst_file = self.env_dir / filename
            if src_file.exists():
                await asyncio.to_thread(shutil.copy2, src_file, dst_file)
                logger.debug(f"Overwrote {filename} from template")
            else:
                logger.warning(f"Template file not found, skipping: {src_file}")

        # Update core files from template
        logger.info(f"Updating core files from template: {template_core_dir}")
        instance_core_dir = self.env_dir / "app" / "core"

        # Remove old core files
        if instance_core_dir.exists():
            await asyncio.to_thread(shutil.rmtree, instance_core_dir)
            logger.debug(f"Removed old core directory: {instance_core_dir}")

        # Copy new core files
        await asyncio.to_thread(
            shutil.copytree,
            template_core_dir,
            instance_core_dir,
            dirs_exist_ok=True
        )
        logger.info(f"Core files updated from template")

        # Update knowledge files from template (add/update only, don't delete)
        template_knowledge_dir = template_dir / "app" / "workspace" / "knowledge"
        instance_knowledge_dir = self.env_dir / "app" / "workspace" / "knowledge"

        if template_knowledge_dir.exists():
            logger.info(f"Updating knowledge files from template: {template_knowledge_dir}")

            # Ensure knowledge directory exists in instance
            instance_knowledge_dir.mkdir(parents=True, exist_ok=True)

            # Copy all knowledge files from template (add/update, preserve user-created files)
            await asyncio.to_thread(
                shutil.copytree,
                template_knowledge_dir,
                instance_knowledge_dir,
                dirs_exist_ok=True
            )
            logger.info(f"Knowledge files updated from template")
        else:
            logger.debug(f"No knowledge directory in template: {template_knowledge_dir}")

        # Rebuild Docker image
        logger.info(f"Rebuilding Docker image for environment {self.env_id}")
        await self._run_compose_command(["build"])
        logger.info(f"Docker image rebuilt successfully")

        # Start container if it was running before
        if was_running:
            logger.info(f"Starting container {self.container_name} after rebuild")
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
                        timestamp=datetime.now(UTC)
                    )
        except httpx.ConnectError as e:
            logger.debug(f"Health check connection error: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"Connection error: {e}",
                timestamp=datetime.now(UTC)
            )
        except httpx.TimeoutException as e:
            logger.debug(f"Health check timeout: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"Timeout: {e}",
                timestamp=datetime.now(UTC)
            )
        except Exception as e:
            logger.warning(f"Health check unexpected error: {type(e).__name__}: {e}")
            return HealthResponse(
                status="unhealthy",
                uptime=0,
                message=f"{type(e).__name__}: {e}",
                timestamp=datetime.now(UTC)
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

    async def get_agent_prompts(self) -> dict[str, str | None]:
        """
        Get agent prompts from docs files.

        Returns:
            Dictionary with 'workflow_prompt', 'entrypoint_prompt', and 'refiner_prompt' keys
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/config/agent-prompts",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                data = response.json()
                return {
                    "workflow_prompt": data.get("workflow_prompt"),
                    "entrypoint_prompt": data.get("entrypoint_prompt"),
                    "refiner_prompt": data.get("refiner_prompt")
                }
        except httpx.HTTPError as e:
            logger.error(f"Failed to get agent prompts: {e}")
            raise Exception(f"Failed to get agent prompts: {e}")

    async def set_agent_prompts(
        self,
        workflow_prompt: str | None = None,
        entrypoint_prompt: str | None = None,
        refiner_prompt: str | None = None
    ) -> bool:
        """
        Update agent prompts in docs files.

        Args:
            workflow_prompt: Content for docs/WORKFLOW_PROMPT.md (None to skip)
            entrypoint_prompt: Content for docs/ENTRYPOINT_PROMPT.md (None to skip)
            refiner_prompt: Content for docs/REFINER_PROMPT.md (None to skip)

        Returns:
            True if successful
        """
        try:
            payload = {}
            if workflow_prompt is not None:
                payload["workflow_prompt"] = workflow_prompt
            if entrypoint_prompt is not None:
                payload["entrypoint_prompt"] = entrypoint_prompt
            if refiner_prompt is not None:
                payload["refiner_prompt"] = refiner_prompt

            if not payload:
                return True  # Nothing to update

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/agent-prompts",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to set agent prompts: {e}")
            raise Exception(f"Failed to set agent prompts: {e}")

    async def set_agent_handover_config(self, handovers: list[dict], handover_prompt: str) -> bool:
        """
        Update agent handover configuration in JSON file.

        Args:
            handovers: List of handover configs with id, name, prompt fields
            handover_prompt: Instructions for handover tool usage in conversation mode

        Returns:
            True if successful
        """
        try:
            payload = {
                "handovers": handovers,
                "handover_prompt": handover_prompt
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/agent-handovers",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to set agent handover config: {e}")
            raise Exception(f"Failed to set agent handover config: {e}")

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

    async def set_credentials(self, credentials_data: dict) -> bool:
        """
        Update credentials in workspace via HTTP API.

        Args:
            credentials_data: Dictionary with keys:
                - credentials_json: List of credentials with full data
                - credentials_readme: Markdown content with redacted credentials

        Returns:
            True if successful
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/credentials",
                    json=credentials_data,
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to set credentials: {e}")
            raise Exception(f"Failed to set credentials: {e}")

    async def set_plugins(self, plugins_data: dict) -> bool:
        """
        Update plugins in workspace via HTTP API.

        Args:
            plugins_data: Dictionary with keys:
                - active_plugins: List of plugin configs
                - settings_json: Settings file content
                - plugin_files: Dict mapping plugin paths to file contents (base64 encoded)

        Returns:
            True if successful
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/config/plugins",
                    json=plugins_data,
                    headers=self._get_headers(),
                    timeout=30.0  # Longer timeout for plugin file transfers
                )
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to set plugins: {e}")
            raise Exception(f"Failed to set plugins: {e}")

    async def get_plugins_settings(self) -> dict:
        """
        Get current plugins settings from environment.

        Returns:
            Current plugins settings dictionary
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/config/plugins/settings",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get plugins settings: {e}")
            raise Exception(f"Failed to get plugins settings: {e}")

    async def upload_file(self, file: File) -> bool:
        """Upload file to container workspace via volume."""
        try:
            # Files are written to workspace/files directory
            file_path = self.env_dir / "app" / "workspace" / "files" / file.path
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
        """Download file from container workspace via volume."""
        file_path = self.env_dir / "app" / "workspace" / "files" / path

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        return File(
            path=path,
            content=file_path.read_bytes(),
            metadata={"size": file_path.stat().st_size}
        )

    async def list_files(self, path: str = "/") -> list[str]:
        """List files in workspace."""
        base_path = self.env_dir / "app" / "workspace" / "files"
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
        file_path = self.env_dir / "app" / "workspace" / "files" / path

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

    async def get_workspace_tree(self) -> dict:
        """
        Get workspace tree via HTTP proxy to agent-env.

        Returns:
            Dictionary with full workspace tree structure
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/workspace/tree",
                    headers=self._get_headers(),
                    timeout=30.0  # Tree building can take time for large workspaces
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get workspace tree: {e}")
            raise Exception(f"Failed to get workspace tree: {e}")

    async def download_workspace_item(self, path: str) -> AsyncIterator[bytes]:
        """
        Download file or folder via HTTP proxy to agent-env.

        Streams response to avoid loading entire file/zip into memory.

        Args:
            path: Relative path from workspace root

        Yields:
            Bytes chunks
        """
        # URL-encode path to handle special characters
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='/')

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "GET",
                    f"{self.base_url}/workspace/download/{encoded_path}",
                    headers=self._get_headers(),
                    timeout=120.0  # Allow time for large zips
                ) as response:
                    response.raise_for_status()

                    # Stream response chunks
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        yield chunk

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Path not found: {path}")
            elif e.response.status_code == 400:
                raise ValueError(f"Invalid path: {path}")
            else:
                logger.error(f"Failed to download workspace item: {e}")
                raise Exception(f"Failed to download workspace item: {e}")
        except httpx.HTTPError as e:
            logger.error(f"Failed to download workspace item: {e}")
            raise Exception(f"Failed to download workspace item: {e}")

    async def install_custom_packages(self) -> bool:
        """
        Install custom Python packages from workspace/workspace_requirements.txt.

        This allows agents to install integration-specific packages (odoo-rpc, salesforce-api, etc.)
        that persist across environment rebuilds.

        Returns:
            True if successful or no packages to install, raises exception on failure
        """
        custom_requirements_path = "/app/workspace/workspace_requirements.txt"

        try:
            # Check if custom requirements file exists
            check_result = await self.execute_command(
                f"test -f {custom_requirements_path} && echo 'exists' || echo 'missing'"
            )

            if "missing" in check_result.stdout:
                logger.debug(f"No custom requirements file found in {self.container_name}")
                return True

            # Count non-empty, non-comment lines
            count_result = await self.execute_command(
                f"grep -v '^#' {custom_requirements_path} | grep -v '^[[:space:]]*$' | wc -l"
            )

            package_count = int(count_result.stdout.strip()) if count_result.stdout.strip().isdigit() else 0

            if package_count == 0:
                logger.debug(f"No custom packages to install in {self.container_name}")
                return True

            # Install packages
            logger.info(f"Installing {package_count} custom package(s) in {self.container_name}")
            install_result = await self.execute_command(
                f"uv pip install -r {custom_requirements_path}"
            )

            if install_result.exit_code != 0:
                logger.error(f"Failed to install custom packages: {install_result.stdout}")
                raise Exception(f"Failed to install custom packages: {install_result.stdout}")

            logger.info(f"Custom packages installed successfully in {self.container_name}")
            return True

        except Exception as e:
            logger.error(f"Error installing custom packages in {self.container_name}: {e}")
            raise

    async def install_system_packages(self) -> bool:
        """
        Install system packages from workspace/workspace_system_packages.txt.

        This allows agents to install OS-level tools (ffmpeg, imagemagick, etc.)
        that persist across environment rebuilds.

        Returns:
            True if successful or no packages to install, raises exception on failure
        """
        system_packages_path = "/app/workspace/workspace_system_packages.txt"

        try:
            check_result = await self.execute_command(
                f"test -f {system_packages_path} && echo 'exists' || echo 'missing'"
            )

            if "missing" in check_result.stdout:
                logger.debug(f"No system packages file found in {self.container_name}")
                return True

            # Read non-empty, non-comment lines as package names
            read_result = await self.execute_command(
                f"grep -v '^#' {system_packages_path} | grep -v '^[[:space:]]*$' | tr '\\n' ' '"
            )

            packages = read_result.stdout.strip()
            if not packages:
                logger.debug(f"No system packages to install in {self.container_name}")
                return True

            logger.info(f"Installing system packages in {self.container_name}: {packages}")
            install_result = await self.execute_command(
                f"apt-get update && apt-get install -y {packages} && rm -rf /var/lib/apt/lists/*"
            )

            if install_result.exit_code != 0:
                logger.error(f"Failed to install system packages: {install_result.stdout}")
                raise Exception(f"Failed to install system packages: {install_result.stdout}")

            logger.info(f"System packages installed successfully in {self.container_name}")
            return True

        except Exception as e:
            logger.error(f"Error installing system packages in {self.container_name}: {e}")
            raise

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

    async def upload_file_to_agent_env(
        self,
        filename: str,
        content: bytes,
    ) -> dict:
        """
        Upload user file to Docker agent-env via HTTP API.

        POST http://{container_name}:{port}/files/upload
        """
        url = f"{self.base_url}/files/upload"

        files = {
            'file': (filename, content, 'application/octet-stream')
        }
        data = {
            'filename': filename
        }
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, files=files, data=data, headers=headers)
            response.raise_for_status()
            return response.json()

    async def upload_files_to_agent_env(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[dict]:
        """
        Upload multiple user files concurrently.

        Uses asyncio.gather for parallel uploads.
        """
        tasks = [self.upload_file_to_agent_env(filename, content) for filename, content in files]
        return await asyncio.gather(*tasks)

    # === Webapp Methods ===

    async def get_webapp_status(self) -> dict:
        """Get webapp metadata from agent-env."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/webapp/status",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get webapp status: {e}")
            raise Exception(f"Failed to get webapp status: {e}")

    async def get_webapp_file(
        self,
        path: str,
        request_headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        """
        Proxy a webapp static file request to agent-env.

        Returns (status_code, response_headers, body).
        Passes through caching headers (If-Modified-Since, If-None-Match, ETag, Last-Modified).
        """
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='/')

        headers = self._get_headers()
        if request_headers:
            for h in ("if-modified-since", "if-none-match"):
                if h in request_headers:
                    headers[h] = request_headers[h]

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/webapp/{encoded_path}",
                    headers=headers,
                    timeout=30.0
                )
                resp_headers = {}
                for h in ("content-type", "etag", "last-modified", "cache-control", "content-length"):
                    if h in response.headers:
                        resp_headers[h] = response.headers[h]

                return response.status_code, resp_headers, response.content
        except httpx.HTTPError as e:
            logger.error(f"Failed to get webapp file {path}: {e}")
            raise Exception(f"Failed to get webapp file: {e}")

    async def call_webapp_api(
        self,
        endpoint: str,
        params: dict | None = None,
        timeout: int = 60,
    ) -> tuple[int, bytes]:
        """
        Proxy a webapp data API call to agent-env.

        Returns (status_code, response_body).
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/webapp/api/{endpoint}",
                    json={"params": params or {}, "timeout": timeout},
                    headers=self._get_headers(),
                    timeout=float(timeout) + 10.0,
                )
                return response.status_code, response.content
        except httpx.HTTPError as e:
            logger.error(f"Failed to call webapp API {endpoint}: {e}")
            raise Exception(f"Failed to call webapp API: {e}")

    # === SQLite Database Methods ===

    async def get_database_tables(self, path: str) -> list[dict]:
        """
        Get list of tables and views from SQLite database.

        Args:
            path: Relative path to SQLite file from workspace root

        Returns:
            List of dicts with 'name' and 'type' keys
        """
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='/')

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/database/tables/{encoded_path}",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Database file not found: {path}")
            elif e.response.status_code == 400:
                raise ValueError(f"Invalid path: {path}")
            else:
                logger.error(f"Failed to get database tables: {e}")
                raise Exception(f"Failed to get database tables: {e}")
        except httpx.HTTPError as e:
            logger.error(f"Failed to get database tables: {e}")
            raise Exception(f"Failed to get database tables: {e}")

    async def get_database_schema(self, path: str) -> dict:
        """
        Get complete schema for SQLite database.

        Args:
            path: Relative path to SQLite file from workspace root

        Returns:
            Dictionary with path, tables, and views (each with columns)
        """
        import urllib.parse
        encoded_path = urllib.parse.quote(path, safe='/')

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/database/schema/{encoded_path}",
                    headers=self._get_headers(),
                    timeout=30.0
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Database file not found: {path}")
            elif e.response.status_code == 400:
                raise ValueError(f"Invalid path: {path}")
            else:
                logger.error(f"Failed to get database schema: {e}")
                raise Exception(f"Failed to get database schema: {e}")
        except httpx.HTTPError as e:
            logger.error(f"Failed to get database schema: {e}")
            raise Exception(f"Failed to get database schema: {e}")

    async def execute_database_query(
        self,
        path: str,
        query: str,
        page: int | None = None,
        page_size: int | None = None,
        timeout_seconds: int = 30
    ) -> dict:
        """
        Execute SQL query on SQLite database.

        Args:
            path: Relative path to SQLite file from workspace root
            query: SQL query to execute
            page: Page number (1-based) for SELECT queries, None = no pagination
            page_size: Number of rows per page, None = no pagination
            timeout_seconds: Query timeout in seconds

        Returns:
            Dictionary with columns, rows, pagination info, and execution stats
        """
        try:
            payload: dict = {
                "path": path,
                "query": query,
                "timeout_seconds": timeout_seconds
            }
            # Only include pagination if both page and page_size are provided
            if page is not None and page_size is not None:
                payload["page"] = page
                payload["page_size"] = page_size

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/database/query",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=float(timeout_seconds) + 5.0  # Add buffer for network
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to execute database query: {e}")
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": "OTHER",
                "rows_affected": None,
                "error": str(e),
                "error_type": "execution_error"
            }
