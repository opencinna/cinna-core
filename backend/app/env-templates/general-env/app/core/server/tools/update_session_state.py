"""
Update Session State Tool for Agent Environment.

This tool allows agents to explicitly declare session outcomes.
It notifies users (even offline) and triggers multi-agent feedback.
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
    "update_session_state",
    "Report the outcome of your current session. Call this when you have finished processing, "
    "need user input, or encountered an error. This notifies the user even if they are offline.",
    {"state": str, "summary": str}
)
async def update_session_state(args: dict[str, Any]) -> dict[str, Any]:
    """
    Report session outcome to the backend.

    Args:
        args: Dictionary with:
            - state: "completed" | "needs_input" | "error"
            - summary: Description of the result, question, or error
    """
    state = args.get("state", "").strip()
    summary = args.get("summary", "").strip()

    # Validate state
    if state not in ("completed", "needs_input", "error"):
        return {
            "content": [{"type": "text", "text": "Error: state must be 'completed', 'needs_input', or 'error'"}],
            "is_error": True
        }

    if not summary:
        return {
            "content": [{"type": "text", "text": "Error: summary is required"}],
            "is_error": True
        }

    # Get backend session ID
    session_id = get_backend_session_id()
    if not session_id:
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
        url = f"{BACKEND_URL}/api/v1/agents/sessions/update-state"
        headers = {
            "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "session_id": session_id,
            "state": state,
            "summary": summary,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    return {
                        "content": [{"type": "text", "text": f"Session state updated to '{state}': {summary}"}]
                    }
                else:
                    error = data.get("error", "Unknown error")
                    return {
                        "content": [{"type": "text", "text": f"Failed to update session state: {error}"}],
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
        logger.error(f"Error in update_session_state: {e}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
            "is_error": True
        }
