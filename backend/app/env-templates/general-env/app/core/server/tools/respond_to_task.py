"""
Respond to Task Tool for Agent Environment.

This tool allows a source agent to send messages to sub-task sessions,
answering clarification requests or providing additional context.
"""
import os
import logging
from typing import Any
import httpx

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

# Environment variables for backend connection
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")

# Import function to get current backend session_id
from ..sdk_manager import get_backend_session_id


@tool(
    "respond_to_task",
    "Send a message to a sub-task's session. Use this to answer clarification requests "
    "from sub-tasks you created, or to provide additional context.",
    {"task_id": str, "message": str}
)
async def respond_to_task(args: dict[str, Any]) -> dict[str, Any]:
    """
    Send a message to a sub-task's session.

    Args:
        args: Dictionary with:
            - task_id: UUID of the sub-task to respond to
            - message: Message content for the target agent
    """
    task_id = args.get("task_id", "").strip()
    message = args.get("message", "").strip()

    if not task_id:
        return {
            "content": [{"type": "text", "text": "Error: task_id is required"}],
            "is_error": True
        }

    if not message:
        return {
            "content": [{"type": "text", "text": "Error: message is required"}],
            "is_error": True
        }

    # Get source session ID for auth verification
    source_session_id = get_backend_session_id()
    if not source_session_id:
        return {
            "content": [{"type": "text", "text": "Error: Backend session ID not available"}],
            "is_error": True
        }

    if not BACKEND_URL or not AGENT_AUTH_TOKEN:
        return {
            "content": [{"type": "text", "text": "Error: Backend connection not configured"}],
            "is_error": True
        }

    try:
        url = f"{BACKEND_URL}/api/v1/agents/tasks/respond"
        headers = {
            "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "task_id": task_id,
            "message": message,
            "source_session_id": source_session_id,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    return {
                        "content": [{"type": "text", "text": f"Message sent to sub-task {task_id}"}]
                    }
                else:
                    error = data.get("error", "Unknown error")
                    return {
                        "content": [{"type": "text", "text": f"Failed to respond to task: {error}"}],
                        "is_error": True
                    }
            else:
                return {
                    "content": [{"type": "text", "text": f"Error: Request failed (HTTP {response.status_code})"}],
                    "is_error": True
                }

    except httpx.TimeoutException:
        return {
            "content": [{"type": "text", "text": "Error: Request timed out"}],
            "is_error": True
        }
    except Exception as e:
        logger.error(f"Error in respond_to_task: {e}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
            "is_error": True
        }
