"""
Collaboration MCP Bridge Server.

Exposes the following tools as an MCP stdio server so that OpenCode agents
can call them via the local MCP server config in opencode.json:
  - create_collaboration
  - post_finding
  - get_collaboration_status

Session context (backend_session_id) is read at call time from:
    /app/core/.opencode/session_context.json

Run with:
    python3 /app/core/server/tools/mcp_bridge/collaboration_server.py
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


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }


def _check_backend_config() -> str | None:
    if not BACKEND_URL:
        return "Error: Backend URL not configured"
    if not AGENT_AUTH_TOKEN:
        return "Error: Authentication token not configured"
    return None


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("collaboration")


@mcp.tool()
def create_collaboration(title: str, subtasks: list, description: str = "") -> str:
    """
    Create a multi-agent collaboration.

    Dispatches subtasks to multiple agents simultaneously. You will receive
    feedback as each subtask completes. Use this when a task requires input
    from multiple agents at the same time.

    Each entry in subtasks must be a dict with:
      - target_agent_id (str UUID): the target agent
      - task_message (str): instructions for the target agent
      - order (int, optional): display ordering

    Args:
        title: Short title for the collaboration.
        subtasks: List of subtask dicts with target_agent_id and task_message.
        description: Longer description of the overall goal (optional).
    """
    title = title.strip()
    description = description.strip() or None

    if not title:
        return "Error: title parameter is required"

    if not subtasks or not isinstance(subtasks, list):
        return "Error: subtasks must be a non-empty list"

    for i, subtask in enumerate(subtasks):
        if not isinstance(subtask, dict):
            return f"Error: subtask {i} must be a dict"
        if not subtask.get("target_agent_id"):
            return f"Error: subtask {i} missing target_agent_id"
        if not subtask.get("task_message", "").strip():
            return f"Error: subtask {i} missing task_message"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agents/collaborations/create",
                json={
                    "title": title,
                    "description": description,
                    "subtasks": subtasks,
                    "source_session_id": source_session_id,
                },
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                collaboration_id = data.get("collaboration_id")
                subtask_count = data.get("subtask_count", 0)
                return (
                    f"Collaboration '{title}' created successfully (ID: {collaboration_id}). "
                    f"Dispatched {subtask_count} subtask(s) to target agents. "
                    f"You will receive feedback as each subtask completes."
                )
            return f"Collaboration creation failed: {data.get('error', 'Unknown error')}"

        if resp.status_code == 401:
            return "Error: Authentication failed"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out"
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in create_collaboration: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def post_finding(collaboration_id: str, finding: str) -> str:
    """
    Post an intermediate finding or observation to a collaboration's shared context.

    Other agents in the collaboration can see all findings via
    get_collaboration_status. Use this to share partial results, discoveries,
    or data that other agents may need.

    Args:
        collaboration_id: UUID of the collaboration.
        finding: The finding text to share.
    """
    collaboration_id = collaboration_id.strip()
    finding = finding.strip()

    if not collaboration_id:
        return "Error: collaboration_id is required"
    if not finding:
        return "Error: finding is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    payload: dict = {"finding": finding}
    if source_session_id:
        payload["source_session_id"] = source_session_id

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agents/collaborations/{collaboration_id}/findings",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                findings_count = len(data.get("findings", []))
                return (
                    f"Finding posted successfully. "
                    f"Total findings in collaboration: {findings_count}."
                )
            return f"Failed to post finding: {data.get('error', 'Unknown error')}"

        if resp.status_code == 401:
            return "Error: Authentication failed"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out"
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in post_finding: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def get_collaboration_status(collaboration_id: str) -> str:
    """
    Check the current status of a multi-agent collaboration.

    Returns overall status, individual subtask statuses, and any findings
    posted by other agents. Use this to monitor progress and read shared results.

    Args:
        collaboration_id: UUID of the collaboration.
    """
    collaboration_id = collaboration_id.strip()

    if not collaboration_id:
        return "Error: collaboration_id is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/v1/agents/collaborations/{collaboration_id}/status",
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "unknown")
            title = data.get("title", "")
            description = data.get("description", "")
            subtasks = data.get("subtasks", [])
            findings = data.get("shared_context", {}).get("findings", [])

            lines = [
                f"## Collaboration: {title}",
                f"**Status**: {status}",
            ]
            if description:
                lines.append(f"**Description**: {description}")

            lines.append(f"\n**Subtasks** ({len(subtasks)} total):")
            for st in subtasks:
                agent_name = st.get("target_agent_name") or st.get("target_agent_id", "Unknown")
                st_status = st.get("status", "unknown")
                result_summary = st.get("result_summary", "")
                summary_str = f" — {result_summary}" if result_summary else ""
                lines.append(f"  - [{st_status.upper()}] {agent_name}{summary_str}")

            if findings:
                lines.append(f"\n**Shared Findings** ({len(findings)} total):")
                for f_item in findings:
                    lines.append(f"  - {f_item}")
            else:
                lines.append("\n**Shared Findings**: None yet")

            return "\n".join(lines)

        if resp.status_code == 404:
            return f"Collaboration {collaboration_id} not found"
        if resp.status_code == 401:
            return "Error: Authentication failed"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out"
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in get_collaboration_status: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")
