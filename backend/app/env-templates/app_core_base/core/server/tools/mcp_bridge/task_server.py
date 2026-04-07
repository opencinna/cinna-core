"""
Agent Task MCP Bridge Server.

Exposes the following tools as an MCP stdio server so that OpenCode agents
can call them via the local MCP server config in opencode.json:
  - add_comment
  - update_status
  - create_task
  - create_subtask
  - get_details
  - list_tasks

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
    """Return an error string if backend is not configured, else None."""
    if not BACKEND_URL:
        return "Error: Backend URL not configured. Cannot process request."
    if not AGENT_AUTH_TOKEN:
        return "Error: Authentication token not configured. Cannot process request."
    return None


def _resolve_task_id(client: httpx.Client, short_code: str) -> tuple[str | None, str | None]:
    """
    Resolve a task short_code to a task_id.

    Returns:
        (task_id, error_string) — exactly one of them is None.
    """
    try:
        resp = client.get(
            f"{BACKEND_URL}/api/v1/agent/tasks/by-code/{short_code}",
            headers=_auth_headers(),
        )
        if resp.status_code == 404:
            return None, f"Error: Task '{short_code}' not found"
        if resp.status_code != 200:
            return None, f"Error: Failed to resolve task '{short_code}' (HTTP {resp.status_code})"
        task_id = resp.json().get("task_id")
        if not task_id:
            return None, f"Error: Could not resolve task ID for '{short_code}'"
        return task_id, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Error: Failed to resolve task: {exc}"


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("agent_task")


@mcp.tool()
def add_comment(
    content: str,
    files: list[str] | None = None,
    task: str = "",
) -> str:
    """
    Post a comment on a task to report findings, results, or progress.

    The primary way to share work with the user and other agents. Optionally
    attach workspace files to the comment. Defaults to the current task if
    'task' is not specified.

    Args:
        content: Comment text (required, markdown supported).
        files: Workspace file paths to attach (optional).
        task: Short code of the target task (optional, defaults to current task).
    """
    content = content.strip()
    task_short_code = task.strip() or None

    if not content:
        return "Error: content is required"

    # Validate file paths exist locally before sending to backend
    if files:
        workspace = Path("/app/workspace")
        missing = []
        resolved_files = []
        for f in files:
            fp = Path(f)
            if not fp.is_absolute():
                fp = workspace / fp
            if not fp.exists():
                missing.append(f)
            else:
                resolved_files.append(str(fp))
        if missing:
            missing_list = "\n".join(f"  - {m}" for m in missing)
            return (
                f"Error: The following files were not found in the workspace:\n"
                f"{missing_list}\n\n"
                f"Fix the file paths and try again. Comment was NOT posted."
            )
        files = resolved_files

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available. Cannot post comment."

    try:
        with httpx.Client(timeout=30.0) as client:
            task_id: str | None = None
            if task_short_code:
                task_id, err = _resolve_task_id(client, task_short_code)
                if err:
                    return err

            url_path = (
                f"/api/v1/agent/tasks/{task_id}/comment"
                if task_id
                else "/api/v1/agent/tasks/current/comment"
            )
            payload: dict = {
                "content": content,
                "source_session_id": source_session_id,
            }
            if files:
                payload["file_paths"] = files

            resp = client.post(
                f"{BACKEND_URL}{url_path}",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            comment_id = data.get("comment_id")
            result_task = data.get("task", task_short_code or "")
            attachments_count = data.get("attachments_count", 0)
            failed_attachments = data.get("failed_attachments", 0)
            parts = [f"Comment posted on task {result_task} (comment_id: {comment_id})."]
            if attachments_count:
                parts.append(f"Attached {attachments_count} file(s).")
            if failed_attachments:
                detail = data.get("message", f"{failed_attachments} file(s) could not be attached.")
                prefix = "Error" if attachments_count == 0 else "WARNING"
                return f"{prefix}: {detail}\n\n" + " ".join(parts)
            return " ".join(parts)

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 404:
            return "Error: Task not found or not accessible."

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in add_comment: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def update_status(
    status: str,
    reason: str = "",
    task: str = "",
) -> str:
    """
    Update a task's status.

    Use ONLY for edge cases: 'blocked' (waiting for external input or a
    dependency), 'completed' (explicit early completion), or 'cancelled'
    (task is not actionable). Standard transitions are handled automatically
    by the backend. Defaults to the current task if 'task' is not specified.

    Args:
        status: New status — blocked/completed/cancelled (required).
        reason: Explanation for the change (optional).
        task: Short code of the target task (optional, defaults to current task).
    """
    status = status.strip()
    reason_val = reason.strip() or None
    task_short_code = task.strip() or None

    allowed = {"blocked", "completed", "cancelled"}
    if not status:
        return "Error: status is required"
    if status not in allowed:
        return f"Error: status must be one of: {', '.join(sorted(allowed))}"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available."

    try:
        with httpx.Client(timeout=30.0) as client:
            task_id: str | None = None
            if task_short_code:
                task_id, err = _resolve_task_id(client, task_short_code)
                if err:
                    return err

            url_path = (
                f"/api/v1/agent/tasks/{task_id}/status"
                if task_id
                else "/api/v1/agent/tasks/current/status"
            )
            payload: dict = {
                "status": status,
                "source_session_id": source_session_id,
            }
            if reason_val:
                payload["reason"] = reason_val

            resp = client.post(
                f"{BACKEND_URL}{url_path}",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            result_task = data.get("task", task_short_code or "")
            previous_status = data.get("previous_status", "")
            new_status = data.get("new_status", status)
            return f"Task {result_task} status updated: {previous_status} → {new_status}."

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 404:
            return "Error: Task not found or not accessible."
        if resp.status_code == 422:
            return f"Error: Invalid status transition — {resp.text}"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in update_status: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def create_task(
    title: str,
    description: str = "",
    assigned_to: str = "",
    priority: str = "",
) -> str:
    """
    Create a new task.

    For team agents, the task inherits the team context. Returns the task's
    short code (e.g. TASK-5 or HR-42).

    Args:
        title: What needs to be done (required).
        description: Detailed context (optional).
        assigned_to: Agent or team member name to assign to (optional).
        priority: low/normal/high/urgent (optional, defaults to normal).
    """
    title = title.strip()
    description_val = description.strip() or None
    assigned_to_val = assigned_to.strip() or None
    priority_val = priority.strip() or None

    if not title:
        return "Error: title is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available."

    payload: dict = {
        "title": title,
        "source_session_id": source_session_id,
    }
    if description_val:
        payload["description"] = description_val
    if assigned_to_val:
        payload["assigned_to"] = assigned_to_val
    if priority_val:
        payload["priority"] = priority_val

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agent/tasks/create",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            task_short_code = data.get("task", "")
            result_assigned_to = data.get("assigned_to")
            parts = [f"Task {task_short_code} created."]
            if result_assigned_to:
                parts.append(f"Assigned to: {result_assigned_to}.")
            return " ".join(parts)

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 422:
            return f"Error: Invalid request — {resp.text}"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in create_task: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def create_subtask(
    title: str,
    description: str = "",
    assigned_to: str = "",
    priority: str = "",
) -> str:
    """
    Create a subtask under the current task and delegate to a team member.

    Only available in team context. Delegation is restricted to connected
    downstream nodes listed in your team context.

    Args:
        title: What needs to be done (required).
        description: Detailed context (optional).
        assigned_to: Team member name to delegate to (optional).
        priority: low/normal/high/urgent (optional, defaults to normal).
    """
    title = title.strip()
    description_val = description.strip() or None
    assigned_to_val = assigned_to.strip() or None
    priority_val = priority.strip() or None

    if not title:
        return "Error: title is required"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available."

    payload: dict = {
        "title": title,
        "source_session_id": source_session_id,
    }
    if description_val:
        payload["description"] = description_val
    if assigned_to_val:
        payload["assigned_to"] = assigned_to_val
    if priority_val:
        payload["priority"] = priority_val

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/v1/agent/tasks/current/subtask",
                json=payload,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            subtask_short_code = data.get("task", "")
            parent_short_code = data.get("parent_task", "")
            result_assigned_to = data.get("assigned_to")
            parts = [f"Subtask {subtask_short_code} created under {parent_short_code}."]
            if result_assigned_to:
                parts.append(f"Assigned to: {result_assigned_to}.")
            return " ".join(parts)

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 403:
            return "Error: Subtask creation requires team context. This tool is only available for team agents."
        if resp.status_code == 404:
            return "Error: Current task not found or not accessible."
        if resp.status_code == 422:
            return f"Error: Invalid request — {resp.text}"

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in create_subtask: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def get_details(task: str = "") -> str:
    """
    Get full details of a task.

    Returns title, description, status, priority, assigned agent, recent
    comments (last 10), subtasks list, and subtask progress. Defaults to the
    current task if 'task' is not specified.

    Args:
        task: Short code of the target task (optional, defaults to current task).
    """
    task_short_code = task.strip() or None

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available."

    try:
        with httpx.Client(timeout=30.0) as client:
            task_id: str | None = None
            if task_short_code:
                task_id, err = _resolve_task_id(client, task_short_code)
                if err:
                    return err

            url_path = (
                f"/api/v1/agent/tasks/{task_id}/details"
                if task_id
                else "/api/v1/agent/tasks/current/details"
            )
            resp = client.get(
                f"{BACKEND_URL}{url_path}",
                params={"source_session_id": source_session_id},
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            lines = [
                f"## Task {data.get('short_code', '')} — {data.get('title', '(no title)')}",
                f"**Status**: {data.get('status', '')}  |  **Priority**: {data.get('priority', 'normal')}",
            ]
            if data.get("assigned_to"):
                lines.append(f"**Assigned to**: {data['assigned_to']}")
            if data.get("created_by"):
                lines.append(f"**Created by**: {data['created_by']}")

            description = data.get("description")
            if description:
                lines.append(f"\n**Description**:\n{description}")

            progress = data.get("subtask_progress") or {}
            if progress.get("total", 0) > 0:
                lines.append(
                    f"\n**Subtask Progress**: {progress.get('completed', 0)}/{progress.get('total', 0)} completed"
                    f" ({progress.get('in_progress', 0)} in progress, {progress.get('blocked', 0)} blocked)"
                )

            subtasks = data.get("subtasks") or []
            if subtasks:
                lines.append(f"\n**Subtasks** ({len(subtasks)}):")
                for st in subtasks:
                    lines.append(
                        f"  - [{st.get('status', '?').upper()}] {st.get('task', '')} — "
                        f"{st.get('title', '')} (assigned: {st.get('assigned_to') or 'unassigned'})"
                    )

            # Task files uploaded to workspace
            uploaded_files = data.get("uploaded_files") or []
            if uploaded_files:
                lines.append(f"\n**Task Files** (uploaded to workspace):")
                for uf in uploaded_files:
                    size_str = f" ({uf['size']} bytes)" if uf.get("size") else ""
                    lines.append(f"  - `{uf['path']}`{size_str}")

            comments = data.get("recent_comments") or []
            if comments:
                lines.append(f"\n**Recent Comments** (last {len(comments)}):")
                for c in comments:
                    author = c.get("author", "unknown")
                    created_at = c.get("created_at", "")
                    has_files = c.get("has_files", False)
                    comment_content = c.get("content", "")
                    file_indicator = " [+files]" if has_files else ""
                    truncated = comment_content[:3000] + ("... [truncated]" if len(comment_content) > 3000 else "")
                    lines.append(
                        f"\n  **{author}** ({created_at}){file_indicator}:\n  {truncated}"
                    )
            else:
                lines.append("\n**Recent Comments**: None")

            return "\n".join(lines)

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 404:
            return "Error: Task not found or not accessible."

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in get_details: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


@mcp.tool()
def list_tasks(
    status: str = "",
    scope: str = "assigned",
) -> str:
    """
    List tasks visible to you.

    Use 'scope' to filter: 'assigned' (tasks assigned to you, default),
    'created' (tasks you created/delegated), 'team' (all tasks in your team
    — team agents only). Optionally filter by status.

    Args:
        status: Filter by status (optional).
        scope: assigned/created/team (optional, defaults to assigned).
    """
    status_filter = status.strip() or None
    scope_val = scope.strip() or "assigned"

    allowed_scopes = {"assigned", "created", "team"}
    if scope_val not in allowed_scopes:
        return f"Error: scope must be one of: {', '.join(sorted(allowed_scopes))}"

    config_error = _check_backend_config()
    if config_error:
        return config_error

    source_session_id = _read_backend_session_id()
    if not source_session_id:
        return "Error: Backend session ID not available."

    params: dict = {
        "scope": scope_val,
        "source_session_id": source_session_id,
    }
    if status_filter:
        params["status"] = status_filter

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/v1/agent/tasks/my-tasks",
                params=params,
                headers=_auth_headers(),
            )

        if resp.status_code == 200:
            data = resp.json()
            tasks = data.get("tasks") or []

            if not tasks:
                filter_desc = f" with status '{status_filter}'" if status_filter else ""
                return f"No tasks found (scope: {scope_val}{filter_desc})."

            lines = [f"## Tasks (scope: {scope_val}, {len(tasks)} found)"]
            for t in tasks:
                short_code = t.get("task", "")
                task_title = t.get("title", "(no title)")
                task_status = t.get("status", "")
                task_priority = t.get("priority", "normal")
                task_assigned = t.get("assigned_to") or "unassigned"

                progress = t.get("subtask_progress") or {}
                progress_str = ""
                if progress.get("total", 0) > 0:
                    progress_str = f" [{progress.get('completed', 0)}/{progress.get('total', 0)} subtasks]"

                priority_indicator = " !" if task_priority == "high" else (" !!" if task_priority == "urgent" else "")
                lines.append(
                    f"\n- **{short_code}**{priority_indicator} [{task_status.upper()}] {task_title}"
                    f"\n  Assigned: {task_assigned}{progress_str}"
                )

            return "\n".join(lines)

        if resp.status_code == 401:
            return "Error: Authentication failed."
        if resp.status_code == 403:
            return "Error: 'team' scope is only available for team agents."

        return f"Error: Request failed (HTTP {resp.status_code}): {resp.text}"

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.RequestError as exc:
        return f"Error: Failed to connect to backend: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in list_tasks: %s", exc, exc_info=True)
        return f"Error: Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")
