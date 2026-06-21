"""
Agent Task Get Details Tool for Agent Environment.

Retrieves full details of a task including title, description, status, priority,
recent comments, subtasks, and subtask progress.
"""
import logging
import os
from typing import Any
import httpx

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")
ENV_ID = os.getenv("ENV_ID", "")  # scopes the auth token to this environment

from ..sdk_manager import get_backend_session_id


@tool(
    "get_details",
    "Get full details of a task: title, description, status, priority, assigned agent, "
    "recent comments (last 10), subtasks list, and subtask progress. "
    "Defaults to the current task if 'task' is not specified.",
    {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Short code of target task (optional, defaults to current task)"},
        },
        "required": [],
    },
)
async def agent_task_get_details(args: dict[str, Any]) -> dict[str, Any]:
    """
    Get full details of a task.

    Args:
        args: Dictionary with:
            - task: Short code of target task (optional, defaults to current task)

    Returns:
        Tool response with task details including comments and subtask progress.
    """
    task_short_code: str | None = args.get("task", "").strip() or None

    if not BACKEND_URL:
        return {
            "content": [{"type": "text", "text": "Error: Backend URL not configured"}],
            "is_error": True,
        }

    if not AGENT_AUTH_TOKEN:
        return {
            "content": [{"type": "text", "text": "Error: Authentication token not configured"}],
            "is_error": True,
        }

    source_session_id = get_backend_session_id()
    if not source_session_id:
        return {
            "content": [{"type": "text", "text": "Error: Backend session ID not available"}],
            "is_error": True,
        }

    headers = {
        "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
        "X-Agent-Env-Id": ENV_ID,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            task_id: str | None = None

            if task_short_code:
                resolve_url = f"{BACKEND_URL}/api/v1/agent/tasks/by-code/{task_short_code}"
                resolve_resp = await client.get(resolve_url, headers=headers)
                if resolve_resp.status_code == 404:
                    return {
                        "content": [{"type": "text", "text": f"Error: Task '{task_short_code}' not found"}],
                        "is_error": True,
                    }
                if resolve_resp.status_code != 200:
                    return {
                        "content": [{
                            "type": "text",
                            "text": f"Error: Failed to resolve task '{task_short_code}' (HTTP {resolve_resp.status_code})",
                        }],
                        "is_error": True,
                    }
                task_id = resolve_resp.json().get("task_id")
                if not task_id:
                    return {
                        "content": [{"type": "text", "text": f"Error: Could not resolve task ID for '{task_short_code}'"}],
                        "is_error": True,
                    }

            url_path = (
                f"/api/v1/agent/tasks/{task_id}/details"
                if task_id
                else "/api/v1/agent/tasks/current/details"
            )
            # Pass session_id as query param so backend can resolve current task
            details_resp = await client.get(
                f"{BACKEND_URL}{url_path}",
                params={"source_session_id": source_session_id},
                headers=headers,
            )

        if details_resp.status_code == 200:
            data = details_resp.json()
            # Format a readable summary
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

            # Subtask progress
            progress = data.get("subtask_progress") or {}
            if progress.get("total", 0) > 0:
                lines.append(
                    f"\n**Subtask Progress**: {progress.get('completed', 0)}/{progress.get('total', 0)} completed"
                    f" ({progress.get('in_progress', 0)} in progress, {progress.get('blocked', 0)} blocked)"
                )

            # Subtasks list
            subtasks = data.get("subtasks") or []
            if subtasks:
                lines.append(f"\n**Subtasks** ({len(subtasks)}):")
                for st in subtasks:
                    lines.append(f"  - [{st.get('status', '?').upper()}] {st.get('task', '')} — {st.get('title', '')} (assigned: {st.get('assigned_to') or 'unassigned'})")

            # Task files uploaded to workspace
            uploaded_files = data.get("uploaded_files") or []
            if uploaded_files:
                lines.append(f"\n**Task Files** (uploaded to workspace):")
                for uf in uploaded_files:
                    size_str = f" ({uf['size']} bytes)" if uf.get("size") else ""
                    lines.append(f"  - `{uf['path']}`{size_str}")

            # Recent comments
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
                    lines.append(f"\n  **{author}** ({created_at}){file_indicator}:\n  {truncated}")
            else:
                lines.append("\n**Recent Comments**: None")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if details_resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if details_resp.status_code == 404:
            return {
                "content": [{"type": "text", "text": "Error: Task not found or not accessible"}],
                "is_error": True,
            }

        logger.error(f"get_details failed HTTP {details_resp.status_code}: {details_resp.text}")
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Request failed (HTTP {details_resp.status_code}): {details_resp.text}",
            }],
            "is_error": True,
        }

    except httpx.TimeoutException:
        return {
            "content": [{"type": "text", "text": "Error: Request timed out"}],
            "is_error": True,
        }
    except httpx.RequestError as exc:
        logger.error(f"Request error in agent_task_get_details: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_get_details: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
