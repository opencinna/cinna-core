"""
Injectable connector for agent-env HTTP streaming communication.

Follows the same DI pattern as smtp_connector / imap_connector:
- Module-level instance used by message_service
- Tests patch the module-level instance with a stub
"""
import json
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class AgentEnvConnector:
    """Wraps httpx for agent-env HTTP communication. Injectable for testing."""

    # Granular timeouts for SSE streaming:
    # - connect/write/pool: short, these are quick operations
    # - read: 30 minutes to support long-running agent sessions where the SDK
    #   may be executing tools for extended periods without emitting SSE events
    STREAM_TIMEOUT = httpx.Timeout(
        connect=30.0,
        read=1800.0,   # 30 minutes
        write=30.0,
        pool=30.0,
    )

    async def stream_chat(
        self,
        base_url: str,
        auth_headers: dict,
        payload: dict,
    ) -> AsyncIterator[dict]:
        """Stream chat events from agent-env /chat/stream endpoint.

        Yields parsed SSE event dicts. Errors are yielded as
        {"type": "error", "content": ..., "error_type": ...} rather than raised,
        matching the existing message_service contract.
        """
        headers = {**auth_headers, "Content-Type": "application/json"}

        logger.info(
            f"Sending message to {base_url}/chat/stream "
            f"(mode={payload.get('mode')}, external_session_id={payload.get('session_id')})"
        )

        try:
            async with httpx.AsyncClient(timeout=self.STREAM_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/stream",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                yield json.loads(data_str)
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to parse SSE event: {data_str}, error: {e}")
                                continue

        except httpx.HTTPStatusError as e:
            try:
                await e.response.aread()
                error_text = e.response.text
            except Exception:
                error_text = "(unable to read response)"
            logger.error(f"HTTP error from environment: {e.response.status_code} - {error_text}")
            yield {
                "type": "error",
                "content": f"Environment returned error: {e.response.status_code}",
                "error_type": "HTTPError",
            }
        except httpx.TimeoutException as e:
            logger.error(f"Timeout streaming from environment: {e}")
            yield {
                "type": "error",
                "content": f"Connection to environment timed out. The agent session may still be running — try sending a new message to reconnect.",
                "error_type": "TimeoutError",
            }
        except httpx.RequestError as e:
            logger.error(f"Request error to environment: {e}")
            yield {
                "type": "error",
                "content": f"Failed to connect to environment: {str(e)}",
                "error_type": "ConnectionError",
            }
        except Exception as e:
            logger.error(f"Unexpected error streaming from environment: {e}", exc_info=True)
            yield {
                "type": "error",
                "content": f"Unexpected error: {str(e)}",
                "error_type": type(e).__name__,
            }

    async def exec_command(
        self,
        base_url: str,
        auth_token: str,
        command: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> dict:
        """Execute a shell command in the agent environment.

        Posts to {base_url}/exec and returns the result dict.

        Args:
            base_url: Environment HTTP base URL.
            auth_token: Bearer token for the agent-env.
            command: Shell command to execute.
            timeout: Command timeout in seconds (max enforced env-side).
            env: Optional extra env vars merged with the subprocess's default
                environment. Values are expected to be strings; callers should
                keep individual values well below the env-side 100 KB cap.
            stdin: Optional input piped to the subprocess's stdin.

        Returns:
            {"exit_code": int, "stdout": str, "stderr": str}

        Raises:
            RuntimeError: On HTTP errors, timeouts, or connection failures.
        """
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }
        # Add extra buffer for HTTP overhead beyond the command timeout
        http_timeout = httpx.Timeout(
            connect=30.0,
            read=float(timeout + 30),
            write=30.0,
            pool=30.0,
        )
        payload: dict = {"command": command, "timeout": timeout}
        if env is not None:
            payload["env"] = env
        if stdin is not None:
            payload["stdin"] = stdin
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                response = await client.post(
                    f"{base_url}/exec",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Environment exec failed: HTTP {e.response.status_code}"
            ) from e
        except httpx.TimeoutException as e:
            raise RuntimeError(
                f"Environment exec timed out after {timeout}s"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(
                f"Failed to connect to environment for exec: {str(e)}"
            ) from e

    async def stream_command(
        self,
        base_url: str,
        auth_headers: dict,
        exec_id: str,
        resolved_command: str,
        timeout: int = 300,
        max_output_bytes: int = 262144,
    ) -> AsyncIterator[dict]:
        """Stream command execution events from agent-env /command/stream endpoint.

        Yields parsed SSE event dicts. Errors are yielded as
        {"type": "error", "content": ..., "error_type": ...} rather than raised,
        matching the existing stream_chat contract.

        Expected event shapes from agent-env:
            {"type": "tool", "tool_name": "bash", ...}
            {"type": "tool_result_delta", "content": "...", "metadata": {...}}
            {"type": "done", "exit_code": N, "duration_seconds": F}
            {"type": "interrupted", "exit_code": -1}
        """
        headers = {**auth_headers, "Content-Type": "application/json"}
        payload = {
            "command": resolved_command,
            "exec_id": exec_id,
            "timeout": timeout,
            "max_output_bytes": max_output_bytes,
        }
        command_timeout = httpx.Timeout(
            connect=30.0,
            read=float(timeout + 60),  # extra buffer beyond command timeout
            write=30.0,
            pool=30.0,
        )

        logger.info(f"Streaming command to {base_url}/command/stream (exec_id={exec_id})")

        try:
            async with httpx.AsyncClient(timeout=command_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/command/stream",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                yield json.loads(data_str)
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to parse command SSE event: {data_str}, error: {e}")
                                continue

        except httpx.HTTPStatusError as e:
            try:
                await e.response.aread()
                error_text = e.response.text
            except Exception:
                error_text = "(unable to read response)"
            logger.error(f"HTTP error from command stream: {e.response.status_code} - {error_text}")
            yield {
                "type": "error",
                "content": f"Environment returned error: {e.response.status_code}",
                "error_type": "HTTPError",
            }
        except httpx.TimeoutException as e:
            logger.error(f"Timeout streaming command from environment: {e}")
            yield {
                "type": "error",
                "content": "Connection to environment timed out during command execution.",
                "error_type": "TimeoutError",
            }
        except httpx.RequestError as e:
            logger.error(f"Request error during command stream: {e}")
            yield {
                "type": "error",
                "content": f"Failed to connect to environment: {str(e)}",
                "error_type": "ConnectionError",
            }
        except Exception as e:
            logger.error(f"Unexpected error during command stream: {e}", exc_info=True)
            yield {
                "type": "error",
                "content": f"Unexpected error: {str(e)}",
                "error_type": type(e).__name__,
            }

    async def open_sync_websocket(self, base_url: str, auth_headers: dict):
        """
        Open a WebSocket connection to the env-core /sync/exec endpoint.

        Returns a websockets.WebSocketClientProtocol (or compatible) object
        connected to the internal env-core sync endpoint.

        Args:
            base_url: Internal base URL of the env-core HTTP server (e.g., http://container:8080)
            auth_headers: Auth headers dict (used for HTTP, not for WS connection itself
                          since this is an internal-only endpoint)

        Returns:
            An open websockets connection object

        Raises:
            RuntimeError: If the connection cannot be established
        """
        import websockets

        # Convert http:// → ws://, https:// → wss://
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url.rstrip('/')}/sync/exec"

        try:
            connection = await websockets.connect(
                ws_url,
                additional_headers=auth_headers,
                open_timeout=15.0,
            )
            logger.info(f"Opened sync WebSocket to env-core at {ws_url}")
            return connection
        except Exception as e:
            raise RuntimeError(f"Failed to connect to env-core sync endpoint at {ws_url}: {e}") from e

    async def open_shell_websocket(
        self,
        base_url: str,
        auth_headers: dict,
        preamble: dict | None = None,
    ):
        """
        Open a WebSocket connection to the env-core /shell/pty endpoint.

        Mirrors ``open_sync_websocket``: converts the internal HTTP base URL to a
        ws:// URL, connects with the env-core auth headers, and optionally sends
        a preamble JSON frame ({"cols": N, "rows": M, "shell": "bash"}) so the
        spawned PTY starts at the right window size.

        Returns a ``websockets`` client connection object connected to the
        internal env-core shell endpoint.

        Args:
            base_url: Internal base URL of the env-core HTTP server (e.g.,
                http://container:8080).
            auth_headers: Auth headers dict — carries the env's AGENT_AUTH_TOKEN
                bearer for the internal-only endpoint.
            preamble: Optional preamble dict sent as a text JSON frame right
                after the connection opens.

        Returns:
            An open websockets connection object.

        Raises:
            RuntimeError: If the connection cannot be established.
        """
        import json as _json

        import websockets

        # Convert http:// → ws://, https:// → wss://
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url.rstrip('/')}/shell/pty"

        try:
            connection = await websockets.connect(
                ws_url,
                additional_headers=auth_headers,
                open_timeout=15.0,
                max_size=None,  # PTY output bursts (TUI redraws) can be large
            )
            logger.info(f"Opened shell WebSocket to env-core at {ws_url}")
        except Exception as e:
            raise RuntimeError(
                f"Failed to connect to env-core shell endpoint at {ws_url}: {e}"
            ) from e

        if preamble is not None:
            try:
                await connection.send(_json.dumps(preamble))
            except Exception as e:
                try:
                    await connection.close()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Failed to send shell preamble to env-core at {ws_url}: {e}"
                ) from e

        return connection

    async def interrupt_command(
        self,
        base_url: str,
        auth_headers: dict,
        exec_id: str,
    ) -> None:
        """Send a command interrupt request to agent-env (fire-and-forget).

        Errors are logged but not raised — the caller should not depend on
        this succeeding (e.g., the process may have already exited).
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{base_url}/command/interrupt/{exec_id}",
                    headers=auth_headers,
                )
                if response.status_code == 200:
                    logger.info(f"Command interrupt sent for exec_id={exec_id}")
                else:
                    logger.warning(
                        f"Command interrupt returned status {response.status_code} "
                        f"for exec_id={exec_id}"
                    )
        except Exception as e:
            logger.warning(f"Failed to send command interrupt for exec_id={exec_id}: {e}")


# Module-level default instance (patched in tests)
agent_env_connector = AgentEnvConnector()
