from abc import ABC, abstractmethod
from typing import AsyncIterator, BinaryIO
from uuid import UUID
from pathlib import Path
from pydantic import BaseModel
from datetime import datetime


class File(BaseModel):
    """Transport-agnostic file abstraction"""
    model_config = {"arbitrary_types_allowed": True}

    path: str
    content: bytes | BinaryIO
    metadata: dict = {}


class EnvInitConfig(BaseModel):
    """Configuration for environment initialization"""
    env_name: str  # e.g., "python-env-basic"
    env_version: str  # e.g., "1.0.0"
    agent_id: UUID
    workspace_id: str  # Unique workspace identifier (env_id)


class MessageRequest(BaseModel):
    """Message request to agent"""
    session_id: UUID
    message: str
    history: list[dict]  # Previous messages
    context: dict  # Session context and metadata


class MessageResponse(BaseModel):
    """Message response from agent"""
    response: str
    metadata: dict | None = None


class HealthResponse(BaseModel):
    """Health check response"""
    status: str  # "healthy" | "degraded" | "unhealthy"
    uptime: int  # Seconds
    message: str | None = None
    timestamp: datetime


class CommandResult(BaseModel):
    """Command execution result"""
    exit_code: int
    stdout: str
    stderr: str


class EnvironmentAdapter(ABC):
    """
    Abstract adapter for environment operations.

    All environment types (Docker, SSH, HTTP, K8s) must implement this interface.
    This provides a transport-agnostic way to interact with agent environments.
    """

    # === Lifecycle Management ===

    @abstractmethod
    async def initialize(self, config: EnvInitConfig) -> bool:
        """
        Initialize the environment.

        Args:
            config: Environment initialization configuration

        Returns:
            True if initialization successful

        Raises:
            Exception if initialization fails
        """
        pass

    @abstractmethod
    async def start(self) -> bool:
        """
        Start the environment (container/process).

        Returns:
            True if started successfully
        """
        pass

    @abstractmethod
    async def stop(self) -> bool:
        """
        Stop the environment gracefully.

        Returns:
            True if stopped successfully
        """
        pass

    @abstractmethod
    async def restart(self) -> bool:
        """
        Restart the environment.

        Returns:
            True if restarted successfully
        """
        pass

    @abstractmethod
    async def delete(self) -> bool:
        """
        Delete the environment and all associated resources.

        This should:
        - Stop the container if running
        - Remove the container
        - Remove volumes
        - Remove networks
        - Clean up any other resources

        Returns:
            True if deletion successful
        """
        pass

    @abstractmethod
    async def health_check(self) -> HealthResponse:
        """
        Check if environment is healthy and responsive.

        Returns:
            HealthResponse with status and details
        """
        pass

    @abstractmethod
    async def get_status(self) -> str:
        """
        Get current environment status.

        Returns:
            Status string: "stopped" | "starting" | "running" | "error"
        """
        pass

    @abstractmethod
    async def rebuild(
        self,
        template_dir: Path,
        template_core_dir: Path,
        rebuild_overwrite_files: list[str],
        was_running: bool
    ) -> bool:
        """
        Rebuild environment with updated core files while preserving workspace.

        This operation:
        1. Stops the container if running
        2. Overwrites infrastructure files from template (Dockerfile, pyproject.toml, etc.)
        3. Updates core system files from template
        4. Rebuilds the Docker image
        5. Starts the container if it was running before

        Args:
            template_dir: Path to template root directory
            template_core_dir: Path to template's core directory
            rebuild_overwrite_files: List of template root files to overwrite in instance
            was_running: Whether container was running before rebuild

        Returns:
            True if rebuild successful

        Raises:
            Exception if rebuild fails
        """
        pass

    # === Configuration Management ===

    @abstractmethod
    async def get_agent_prompts(self) -> dict[str, str | None]:
        """
        Get agent prompts from docs files.

        Returns:
            Dictionary with 'workflow_prompt' and 'entrypoint_prompt' keys
        """
        pass

    @abstractmethod
    async def set_agent_prompts(self, workflow_prompt: str | None = None, entrypoint_prompt: str | None = None) -> bool:
        """
        Update agent prompts in docs files.

        Args:
            workflow_prompt: Content for docs/WORKFLOW_PROMPT.md (None to skip)
            entrypoint_prompt: Content for docs/ENTRYPOINT_PROMPT.md (None to skip)

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def set_agent_handover_config(self, handovers: list[dict], handover_prompt: str) -> bool:
        """
        Update agent handover configuration in JSON file.

        Args:
            handovers: List of handover configs with id, name, prompt fields
            handover_prompt: Instructions for handover tool usage in conversation mode

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def set_config(self, config: dict) -> bool:
        """
        Set or update environment configuration.

        Args:
            config: Key-value configuration (env vars, settings, etc.)

        Returns:
            True if config set successfully
        """
        pass

    @abstractmethod
    async def set_credentials(self, credentials: list[dict]) -> bool:
        """
        Set or update credentials in the environment.

        Args:
            credentials: List of decrypted credentials to mount/inject

        Returns:
            True if credentials set successfully
        """
        pass

    @abstractmethod
    async def set_plugins(self, plugins_data: dict) -> bool:
        """
        Set or update plugins in the environment.

        Args:
            plugins_data: Dictionary containing:
                - active_plugins: List of plugin configs
                - settings_json: Settings file content
                - plugin_files: Dict mapping plugin paths to file contents

        Returns:
            True if plugins set successfully
        """
        pass

    @abstractmethod
    async def get_plugins_settings(self) -> dict:
        """
        Get current plugins settings from environment.

        Returns:
            Current plugins settings dictionary
        """
        pass

    # === File Operations ===

    @abstractmethod
    async def upload_file(self, file: File) -> bool:
        """
        Upload a file to the environment's workspace.

        Args:
            file: File object (path, content, metadata)

        Returns:
            True if upload successful
        """
        pass

    @abstractmethod
    async def download_file(self, path: str) -> File:
        """
        Download a file from the environment's workspace.

        Args:
            path: Path to file in environment

        Returns:
            File object with content
        """
        pass

    @abstractmethod
    async def list_files(self, path: str = "/") -> list[str]:
        """
        List files in environment's workspace.

        Args:
            path: Directory path to list

        Returns:
            List of file paths
        """
        pass

    @abstractmethod
    async def delete_file(self, path: str) -> bool:
        """
        Delete a file from the environment's workspace.

        Args:
            path: Path to file to delete

        Returns:
            True if deletion successful
        """
        pass

    @abstractmethod
    async def get_workspace_tree(self) -> dict:
        """
        Get complete workspace directory tree structure.

        Returns:
            Dictionary with workspace tree (files, logs, scripts, docs) and summaries
            Format matches WorkspaceTreeResponse from agent-env
        """
        pass

    @abstractmethod
    async def download_workspace_item(self, path: str) -> AsyncIterator[bytes]:
        """
        Download a file or folder from workspace (as zip if folder).

        Args:
            path: Relative path from workspace root (e.g., "files/data.csv")

        Yields:
            Bytes chunks for streaming download

        Raises:
            FileNotFoundError: Path doesn't exist
            ValueError: Invalid path (directory traversal attempt)
            Exception: Download failed
        """
        pass

    # === Message Communication ===

    @abstractmethod
    async def send_message(self, request: MessageRequest) -> MessageResponse:
        """
        Send a message to the agent and get response.

        Args:
            request: Message request with session_id, message, history, context

        Returns:
            Agent's response message
        """
        pass

    @abstractmethod
    async def stream_message(self, request: MessageRequest) -> AsyncIterator[str]:
        """
        Stream agent response in chunks (for real-time UI).

        Args:
            request: Message request

        Yields:
            Response chunks as they're generated
        """
        pass

    # === Command Execution ===

    @abstractmethod
    async def execute_command(self, command: str, timeout: int = 60) -> CommandResult:
        """
        Execute a command in the environment.

        Args:
            command: Shell command to execute
            timeout: Command timeout in seconds

        Returns:
            Command result with exit code, stdout, stderr
        """
        pass

    # === Logs & Monitoring ===

    @abstractmethod
    async def get_logs(self, lines: int = 100, follow: bool = False) -> list[str] | AsyncIterator[str]:
        """
        Get logs from the environment.

        Args:
            lines: Number of recent log lines to retrieve
            follow: If True, stream logs in real-time

        Returns:
            List of log lines, or async iterator if follow=True
        """
        pass

    # === User File Upload (via HTTP API) ===

    @abstractmethod
    async def upload_file_to_agent_env(
        self,
        filename: str,
        content: bytes,
    ) -> dict:
        """
        Upload user file to agent environment via HTTP API.

        This uploads files through the agent-env's file upload endpoint,
        which handles sanitization, conflict resolution, and proper placement
        in the workspace/uploads/ directory.

        Args:
            filename: Suggested filename (agent-env may sanitize/rename)
            content: File bytes

        Returns:
            dict with:
                "path": str (e.g., "./uploads/filename.pdf"),
                "filename": str (final filename, may differ if conflict),
                "size": int (bytes)
        """
        pass

    @abstractmethod
    async def upload_files_to_agent_env(
        self,
        files: list[tuple[str, bytes]],  # [(filename, content), ...]
    ) -> list[dict]:
        """
        Upload multiple user files to agent environment (batch operation).

        Uploads files concurrently via HTTP API for better performance.

        Args:
            files: List of (filename, content) tuples

        Returns:
            List of file info dicts (same format as upload_file_to_agent_env)
        """
        pass
