"""
Task MCP Bridge Server.

Exposes the following tools as an MCP stdio server so that OpenCode agents
can call them via the local MCP server config in opencode.json:
  - create_agent_task
  - update_session_state
  - respond_to_task

Session context (backend_session_id) is read at call time from:
    /app/core/.opencode/session_context.json

This file is written by the OpenCodeAdapter before each message send, so the
bridge always has access to the current session.

Run with:
    python3 /app/core/server/tools/mcp_bridge/task_server.py
"""

import json
import logging
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN", "")

# Session context is written by the adapter into the opencode serve runtime dir.
# MCP bridge servers are spawned by opencode serve with cwd = runtime dir,
# so reading from cwd works for per-mode dirs (/tmp/.opencode_{mode}).
SESSION_CONTEXT_PATH = Path("session_context.json")
HANDOVER_CONFIG_PATH = Path("/app/workspace/docs/agent_handover_config.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_backend_session_id() -> str | None:
    """Read the current backend_session_id from the session context file."""
    try:
        if SESSION_CONTEXT_PATH.exists():
            data = json.loads(SESSION_CONTEXT_PATH.read_text(encoding="utf-8"))
            return data.get("backend_session_id") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read session_context.json: %s", exc)
    return None


def _load_handover_config() -> dict:
    """Load handover configuration from the workspace docs directory."""
    try:
        if HANDOVER_CONFIG_PATH.exists():
            return json.loads(HANDOVER_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load handover config: %s", exc)
    return {"handovers": []}


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }


def _check_backend_config() -> str | None:
    """Return an error string if backend is not configured, else None."""
    if not BACKEND_URL:
        return "Error: Backend URL not configured. Cannot process request."
    if not AGENT_AUTH_TOKEN:
        return "Error: Authentication token not configured. Cannot process request."
    return None


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("task")


@mcp.tool()
def create_agent_task(
    task_message: str,
    target_agent_id: str = "",
    target_agent_name: str = "",
) -> str:
    """
    Create a task for another agent (direct handover) or for the user's inbox.

    Use with target_agent_id for direct handover to a configured agent.
    Use without target_agent_id to create an inbox task for user review.

    Args:
        task_message: The task description or message (required).
        target_agent_id: UUID of the target agent (optional, for direct handover).
        target_agent_name: Name of the target agent (required if target_agent_id provided).
    """
    task_message = task_message.strip()
    target_agent_id = target_agent_id.strip()
    target_agent_name = target_agent_name.strip()

    if not task_message:
        return "Error: task_message parameter is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    if target_agent_id:
        if not target_agent_name:
            return "Error: target_agent_name is required when target_agent_id is provided"

        config = _load_handover_config()
        configured_ids = [h.get("id") for h in config.get("handovers", [])]
        if target_agent_id not in configured_ids:
            available = ", ".join(
                h.get("name", "Unknown") for h in config.get("handovers", [])
            )
            return (
                f"Error: Handover to agent '{target_agent_name}' ({target_agent_id}) "
                f"is not configured. Available handovers: {available}"
            )

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available. Cannot create task."

    payload: dict = {
        "task_message": task_message,
        "source_session_id": source_session_id,
    }
    if target_agent_id:
        payload["target_agent_id"] = target_agent_id
        payload["target_agent_name"] = target_agent_name

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agents/tasks/create",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return data.get(
                    "message",
                    (
                        f"Successfully handed over to agent '{target_agent_name}'."
                        if target_agent_id
                        else "Task created in user's inbox."
                    ),
                )
            return f"Task creation failed: {data.get('error', 'Unknown error')}"

        if resp.status_code == 401:
            return "Error: Authentication failed. Invalid authentication token."

        return f"Error: Task creation request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Task creation request timed out. Please try again."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in create_agent_task: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def update_session_state(state: str, summary: str) -> str:
    """
    Report the outcome of the current session.

    Call this when you have finished processing, need user input, or encountered
    an error. This notifies the user even if they are offline.

    Args:
        state: One of "completed", "needs_input", or "error".
        summary: Description of the result, question, or error.
    """
    state = state.strip()
    summary = summary.strip()

    if state not in ("completed", "needs_input", "error"):
        return "Error: state must be 'completed', 'needs_input', or 'error'"

    if not summary:
        return "Error: summary is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    session_id = _read_backend_session_id()
    if not session_id:
        return "Error: Backend session ID not available"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agents/sessions/update-state",
                json={"session_id": session_id, "state": state, "summary": summary},
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return f"Session state updated to '{state}': {summary}"
            return f"Failed to update session state: {data.get('error', 'Unknown error')}"

        return f"Error: Request failed (HTTP {resp.status_code})"

    except httpx.TimeoutException:
        return "Error: Request timed out"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in update_session_state: %s", exc, exc_info=True)
        return f"Error: {exc}"


@mcp.tool()
def respond_to_task(task_id: str, message: str) -> str:
    """
    Send a message to a sub-task's session.

    Use this to answer clarification requests from sub-tasks you created, or to
    provide additional context.

    Args:
        task_id: UUID of the sub-task to respond to.
        message: Message content for the target agent.
    """
    task_id = task_id.strip()
    message = message.strip()

    if not task_id:
        return "Error: task_id is required"
    if not message:
        return "Error: message is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agents/tasks/respond",
                json={
                    "task_id": task_id,
                    "message": message,
                    "source_session_id": source_session_id,
                },
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return f"Message sent to sub-task {task_id}"
            return f"Failed to respond to task: {data.get('error', 'Unknown error')}"

        return f"Error: Request failed (HTTP {resp.status_code})"

    except httpx.TimeoutException:
        return "Error: Request timed out"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in respond_to_task: %s", exc, exc_info=True)
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")
