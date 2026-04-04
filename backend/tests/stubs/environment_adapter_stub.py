"""
Test adapter for EnvironmentAdapter.

Follows the email stubs pattern (StubSMTPConnector.sent_emails, StubIMAPConnector.connect_calls).
Implements all abstract methods and Docker-specific methods used by lifecycle manager.
Tracks all interactions for test assertions.
"""
from pathlib import Path
from typing import AsyncIterator
from datetime import datetime, UTC

from app.services.environments.adapters.base import (
    EnvironmentAdapter,
    EnvInitConfig,
    File,
    MessageRequest,
    MessageResponse,
    HealthResponse,
    CommandResult,
)


class EnvironmentTestAdapter(EnvironmentAdapter):
    """Stub adapter that tracks calls instead of performing real operations."""

    def __init__(self, **kwargs):
        self._status = "stopped"
        self.initialize_calls: list[EnvInitConfig] = []
        self.start_calls: int = 0
        self.stop_calls: int = 0
        self.restart_calls: int = 0
        self.delete_calls: int = 0
        self.rebuild_calls: list[dict] = []
        self.prompts_set: dict = {}
        self.credentials_set: list = []
        self.plugins_set: dict = {}
        self.handover_config_set: dict = {}
        self.config_set: dict = {}
        self.uploaded_files: list[File] = []

    # --- Lifecycle ---

    async def initialize(self, config: EnvInitConfig) -> bool:
        self.initialize_calls.append(config)
        return True

    async def start(self) -> bool:
        self._status = "running"
        self.start_calls += 1
        return True

    async def stop(self) -> bool:
        self._status = "stopped"
        self.stop_calls += 1
        return True

    async def restart(self) -> bool:
        self._status = "running"
        self.restart_calls += 1
        return True

    async def delete(self) -> bool:
        self._status = "stopped"
        self.delete_calls += 1
        return True

    async def health_check(self) -> HealthResponse:
        return HealthResponse(
            status="healthy", uptime=0, timestamp=datetime.now(UTC)
        )

    async def get_status(self) -> str:
        return self._status

    async def rebuild(
        self,
        template_dir: Path,
        template_core_dir: Path,
        rebuild_overwrite_files: list[str],
        was_running: bool,
    ) -> bool:
        self.rebuild_calls.append({
            "template_dir": template_dir,
            "template_core_dir": template_core_dir,
            "rebuild_overwrite_files": rebuild_overwrite_files,
            "was_running": was_running,
        })
        return True

    # --- Configuration ---

    async def get_agent_prompts(self) -> dict[str, str | None]:
        return self.prompts_set or {
            "workflow_prompt": None,
            "entrypoint_prompt": None,
            "refiner_prompt": None,
        }

    async def set_agent_prompts(
        self,
        workflow_prompt: str | None = None,
        entrypoint_prompt: str | None = None,
        refiner_prompt: str | None = None,
    ) -> bool:
        if workflow_prompt is not None:
            self.prompts_set["workflow_prompt"] = workflow_prompt
        if entrypoint_prompt is not None:
            self.prompts_set["entrypoint_prompt"] = entrypoint_prompt
        if refiner_prompt is not None:
            self.prompts_set["refiner_prompt"] = refiner_prompt
        return True

    async def set_agent_handover_config(
        self, handovers: list[dict], handover_prompt: str
    ) -> bool:
        self.handover_config_set = {
            "handovers": handovers,
            "handover_prompt": handover_prompt,
        }
        return True

    async def set_config(self, config: dict) -> bool:
        self.config_set.update(config)
        return True

    async def set_credentials(self, credentials: list[dict]) -> bool:
        self.credentials_set = credentials
        return True

    async def set_plugins(self, plugins_data: dict) -> bool:
        self.plugins_set = plugins_data
        return True

    async def get_plugins_settings(self) -> dict:
        return {}

    # --- File Operations ---

    async def upload_file(self, file: File) -> bool:
        self.uploaded_files.append(file)
        return True

    async def download_file(self, path: str) -> File:
        return File(path=path, content=b"")

    async def list_files(self, path: str = "/") -> list[str]:
        return []

    async def delete_file(self, path: str) -> bool:
        return True

    async def get_workspace_tree(self) -> dict:
        return {}

    async def download_workspace_item(self, path: str) -> AsyncIterator[bytes]:
        yield b""

    # --- Messages ---

    async def send_message(self, request: MessageRequest) -> MessageResponse:
        return MessageResponse(response="stub response")

    async def stream_message(self, request: MessageRequest) -> AsyncIterator[str]:
        async def _stream():
            yield "stub response"
        return _stream()

    # --- Commands ---

    async def execute_command(self, command: str, timeout: int = 60) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="")

    # --- Logs ---

    async def get_logs(
        self, lines: int = 100, follow: bool = False
    ) -> list[str] | AsyncIterator[str]:
        return []

    # --- User File Upload ---

    async def upload_file_to_agent_env(self, filename: str, content: bytes) -> dict:
        return {"path": f"./uploads/{filename}", "filename": filename, "size": len(content)}

    async def upload_files_to_agent_env(
        self, files: list[tuple[str, bytes]]
    ) -> list[dict]:
        return [
            {"path": f"./uploads/{name}", "filename": name, "size": len(data)}
            for name, data in files
        ]

    # --- Docker-specific (used by lifecycle manager) ---

    def get_container(self):
        """Used by _container_exists. Returns None = no container."""
        return None

    async def install_custom_packages(self) -> bool:
        return True

    async def install_system_packages(self) -> bool:
        return True

    # --- Webapp ---

    webapp_has_index: bool = True

    async def get_webapp_status(self) -> dict:
        return {
            "exists": self.webapp_has_index,
            "total_size_bytes": 1024 if self.webapp_has_index else 0,
            "file_count": 3 if self.webapp_has_index else 0,
            "has_index": self.webapp_has_index,
            "api_endpoints": ["get_data"] if self.webapp_has_index else [],
        }

    async def get_webapp_file(
        self,
        path: str,
        request_headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        if not self.webapp_has_index and path == "index.html":
            return 404, {"content-type": "application/json"}, b'{"detail":"File not found: index.html"}'
        if path == "index.html":
            return 200, {"content-type": "text/html", "cache-control": "no-cache"}, b"<html>Hello</html>"
        return 200, {"content-type": "application/octet-stream", "cache-control": "no-cache"}, b"file content"

    async def call_webapp_api(
        self,
        endpoint: str,
        params: dict | None = None,
        timeout: int = 60,
    ) -> tuple[int, bytes]:
        import json
        return 200, json.dumps({"result": "ok", "endpoint": endpoint}).encode()
