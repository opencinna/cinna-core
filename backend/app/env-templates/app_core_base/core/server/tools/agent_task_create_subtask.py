"""
Agent Task Create Subtask Tool for Agent Environment.

Allows a team agent to create a subtask under the current task and delegate it
to a connected downstream team member. Only available in team context.
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
    "create_subtask",
    "Create a subtask under the current task and delegate it to a connected team member. "
    "Only available in team context. Delegation is restricted to connected downstream nodes "
    "listed in your team context. "
    "Returns the subtask short code and parent task short code.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What needs to be done (required)"},
            "description": {"type": "string", "description": "Detailed context (optional)"},
            "assigned_to": {"type": "string", "description": "Team member name to delegate to (optional)"},
            "priority": {"type": "string", "description": "low/normal/high/urgent (optional, defaults to normal)"},
        },
        "required": ["title"],
    },
)
async def agent_task_create_subtask(args: dict[str, Any]) -> dict[str, Any]:
    """
    Create a subtask under the current task.

    Args:
        args: Dictionary with:
            - title: What needs to be done (required)
            - description: Detailed context (optional)
            - assigned_to: Team member name to delegate to (optional)
            - priority: low/normal/high/urgent (optional, defaults to normal)

    Returns:
        Tool response with subtask short_code, parent task short_code, and assigned_to.
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
                f"{BACKEND_URL}/api/v1/agent/tasks/current/subtask",
                json=payload,
                headers=headers,
            )

        if resp.status_code == 200:
            data = resp.json()
            subtask_short_code = data.get("task", "")
            parent_short_code = data.get("parent_task", "")
            result_assigned_to = data.get("assigned_to")
            logger.info(
                f"Subtask created: {subtask_short_code} under {parent_short_code}, "
                f"assigned_to={result_assigned_to}"
            )
            parts = [f"Subtask {subtask_short_code} created under {parent_short_code}."]
            if result_assigned_to:
                parts.append(f"Assigned to: {result_assigned_to}.")
            return {"content": [{"type": "text", "text": " ".join(parts)}]}

        if resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if resp.status_code == 403:
            return {
                "content": [{
                    "type": "text",
                    "text": "Error: Subtask creation requires team context. This tool is only available for team agents.",
                }],
                "is_error": True,
            }
        if resp.status_code == 404:
            return {
                "content": [{"type": "text", "text": "Error: Current task not found or not accessible"}],
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

        logger.error(f"create_subtask failed HTTP {resp.status_code}: {resp.text}")
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
        logger.error(f"Request error in agent_task_create_subtask: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_create_subtask: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
