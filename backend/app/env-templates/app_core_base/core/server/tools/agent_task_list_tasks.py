"""
Agent Task List Tasks Tool for Agent Environment.

Lists tasks visible to the agent, filtered by status and scope.
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

ALLOWED_SCOPES = frozenset(["assigned", "created", "team"])


@tool(
    "list_tasks",
    "List tasks visible to you. Use 'scope' to filter: "
    "'assigned' (tasks assigned to you, default), "
    "'created' (tasks you created/delegated), "
    "'team' (all tasks in your team — team agents only). "
    "Optionally filter by status.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status (optional)"},
            "scope": {"type": "string", "description": "assigned/created/team (optional, defaults to assigned)"},
        },
        "required": [],
    },
)
async def agent_task_list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """
    List tasks visible to the agent.

    Args:
        args: Dictionary with:
            - status: Filter by status (optional)
            - scope: assigned/created/team (optional, defaults to assigned)

    Returns:
        Tool response with list of tasks.
    """
    status_filter: str | None = args.get("status", "").strip() or None
    scope: str = args.get("scope", "assigned").strip() or "assigned"

    if scope not in ALLOWED_SCOPES:
        return {
            "content": [{
                "type": "text",
                "text": f"Error: scope must be one of: {', '.join(sorted(ALLOWED_SCOPES))}",
            }],
            "is_error": True,
        }

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

    params: dict[str, str] = {
        "scope": scope,
        "source_session_id": source_session_id,
    }
    if status_filter:
        params["status"] = status_filter

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/agent/tasks/my-tasks",
                params=params,
                headers=headers,
            )

        if resp.status_code == 200:
            data = resp.json()
            tasks = data.get("tasks") or []

            if not tasks:
                filter_desc = f" with status '{status_filter}'" if status_filter else ""
                return {
                    "content": [{
                        "type": "text",
                        "text": f"No tasks found (scope: {scope}{filter_desc}).",
                    }],
                }

            lines = [f"## Tasks (scope: {scope}, {len(tasks)} found)"]
            for task in tasks:
                short_code = task.get("task", "")
                task_title = task.get("title", "(no title)")
                task_status = task.get("status", "")
                task_priority = task.get("priority", "normal")
                task_assigned = task.get("assigned_to") or "unassigned"

                progress = task.get("subtask_progress") or {}
                progress_str = ""
                if progress.get("total", 0) > 0:
                    progress_str = f" [{progress.get('completed', 0)}/{progress.get('total', 0)} subtasks]"

                priority_indicator = ""
                if task_priority == "high":
                    priority_indicator = " !"
                elif task_priority == "urgent":
                    priority_indicator = " !!"

                lines.append(
                    f"\n- **{short_code}**{priority_indicator} [{task_status.upper()}] {task_title}"
                    f"\n  Assigned: {task_assigned}{progress_str}"
                )

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if resp.status_code == 403:
            return {
                "content": [{
                    "type": "text",
                    "text": "Error: 'team' scope is only available for team agents.",
                }],
                "is_error": True,
            }

        logger.error(f"list_tasks failed HTTP {resp.status_code}: {resp.text}")
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Request failed (HTTP {resp.status_code}): {resp.text}",
            }],
            "is_error": True,
        }

    except httpx.TimeoutException:
        return {
            "content": [{"type": "text", "text": "Error: Request timed out"}],
            "is_error": True,
        }
    except httpx.RequestError as exc:
        logger.error(f"Request error in agent_task_list_tasks: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_list_tasks: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
