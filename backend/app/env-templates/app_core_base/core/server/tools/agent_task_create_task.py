"""
Agent Task Create Task Tool for Agent Environment.

Allows an agent to create a new standalone task. For team agents, the task
inherits the team context. Replaces the old create_agent_task tool.
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
    "create_task",
    "Create a new task. Optionally assign it to an agent by name. "
    "For team agents, the task inherits the team context. "
    "Returns the task's short code (e.g. TASK-5 or HR-42).",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What needs to be done (required)"},
            "description": {"type": "string", "description": "Detailed context (optional)"},
            "assigned_to": {"type": "string", "description": "Agent or team member name to assign to (optional)"},
            "priority": {"type": "string", "description": "low/normal/high/urgent (optional, defaults to normal)"},
        },
        "required": ["title"],
    },
)
async def agent_task_create_task(args: dict[str, Any]) -> dict[str, Any]:
    """
    Create a new task.

    Args:
        args: Dictionary with:
            - title: What needs to be done (required)
            - description: Detailed context (optional)
            - assigned_to: Agent or team member name to assign to (optional)
            - priority: low/normal/high/urgent (optional, defaults to normal)

    Returns:
        Tool response with task short_code and assigned_to.
    """
    title = args.get("title", "").strip()
    description: str | None = args.get("description", "").strip() or None
    assigned_to: str | None = args.get("assigned_to", "").strip() or None
    priority: str | None = args.get("priority", "").strip() or None

    if not title:
        return {
            "content": [{"type": "text", "text": "Error: title is required"}],
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

    payload: dict[str, Any] = {
        "title": title,
        "source_session_id": source_session_id,
    }
    if description:
        payload["description"] = description
    if assigned_to:
        payload["assigned_to"] = assigned_to
    if priority:
        payload["priority"] = priority

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/agent/tasks/create",
                json=payload,
                headers=headers,
            )

        if resp.status_code == 200:
            data = resp.json()
            task_short_code = data.get("task", "")
            result_assigned_to = data.get("assigned_to")
            logger.info(f"Task created: {task_short_code}, assigned_to={result_assigned_to}")
            parts = [f"Task {task_short_code} created."]
            if result_assigned_to:
                parts.append(f"Assigned to: {result_assigned_to}.")
            return {"content": [{"type": "text", "text": " ".join(parts)}]}

        if resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if resp.status_code == 422:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: Invalid request — {resp.text}",
                }],
                "is_error": True,
            }

        logger.error(f"create_task failed HTTP {resp.status_code}: {resp.text}")
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
        logger.error(f"Request error in agent_task_create_task: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_create_task: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
