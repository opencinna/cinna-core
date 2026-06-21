"""
Agent Task Update Status Tool for Agent Environment.

Allows an agent to explicitly update a task's status. This is optional —
the backend manages standard status transitions automatically from the session
lifecycle (started → in_progress, completed → completed, error → error).

Use this only for edge cases the backend cannot infer:
- blocked: waiting for external input or a dependency
- completed: explicit early completion before the session ends
- cancelled: task is not actionable
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

ALLOWED_STATUSES = frozenset(["blocked", "completed", "cancelled"])


@tool(
    "update_status",
    "Update a task's status. Use ONLY for edge cases: 'blocked' (waiting for external input), "
    "'completed' (explicit early completion), or 'cancelled' (task not actionable). "
    "Standard transitions (in_progress, auto-complete on session end) are handled automatically. "
    "Defaults to the current task if 'task' is not specified.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "New status — blocked/completed/cancelled (required)"},
            "reason": {"type": "string", "description": "Explanation for the change (optional)"},
            "task": {"type": "string", "description": "Short code of target task (optional, defaults to current task)"},
        },
        "required": ["status"],
    },
)
async def agent_task_update_status(args: dict[str, Any]) -> dict[str, Any]:
    """
    Update a task's status.

    Args:
        args: Dictionary with:
            - status: New status — blocked/completed/cancelled (required)
            - reason: Explanation for the change (optional)
            - task: Short code of target task (optional, defaults to current task)

    Returns:
        Tool response with task short_code, previous_status, and new_status.
    """
    status = args.get("status", "").strip()
    reason: str | None = args.get("reason", "").strip() or None
    task_short_code: str | None = args.get("task", "").strip() or None

    if not status:
        return {
            "content": [{"type": "text", "text": "Error: status is required"}],
            "is_error": True,
        }

    if status not in ALLOWED_STATUSES:
        return {
            "content": [{
                "type": "text",
                "text": f"Error: status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}",
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
                f"/api/v1/agent/tasks/{task_id}/status"
                if task_id
                else "/api/v1/agent/tasks/current/status"
            )
            payload: dict[str, Any] = {
                "status": status,
                "source_session_id": source_session_id,
            }
            if reason:
                payload["reason"] = reason

            status_resp = await client.post(
                f"{BACKEND_URL}{url_path}",
                json=payload,
                headers=headers,
            )

        if status_resp.status_code == 200:
            data = status_resp.json()
            result_task = data.get("task", task_short_code or "")
            previous_status = data.get("previous_status", "")
            new_status = data.get("new_status", status)
            logger.info(f"Task {result_task} status updated: {previous_status} → {new_status}")
            return {
                "content": [{
                    "type": "text",
                    "text": f"Task {result_task} status updated: {previous_status} → {new_status}.",
                }],
            }

        if status_resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if status_resp.status_code == 404:
            return {
                "content": [{"type": "text", "text": "Error: Task not found or not accessible"}],
                "is_error": True,
            }
        if status_resp.status_code == 422:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: Invalid status transition — {status_resp.text}",
                }],
                "is_error": True,
            }

        logger.error(f"update_status failed HTTP {status_resp.status_code}: {status_resp.text}")
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Request failed (HTTP {status_resp.status_code}): {status_resp.text}",
            }],
            "is_error": True,
        }

    except httpx.TimeoutException:
        return {
            "content": [{"type": "text", "text": "Error: Request timed out"}],
            "is_error": True,
        }
    except httpx.RequestError as exc:
        logger.error(f"Request error in agent_task_update_status: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_update_status: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
