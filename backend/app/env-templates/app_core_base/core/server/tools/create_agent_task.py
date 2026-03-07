"""
Create Agent Task Tool for Agent Environment.

This tool allows a conversational agent to create tasks in two modes:
1. Direct handover: Specify target agent → task auto-executes
2. Inbox task: No target agent → goes to user's inbox for refinement
"""
import os
import json
import logging
from typing import Any
import httpx

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

# Environment variables for backend connection
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")
AGENT_ID = os.getenv("AGENT_ID")

# Path to handover config file in workspace
HANDOVER_CONFIG_PATH = "/app/workspace/docs/agent_handover_config.json"

# Import functions to get current backend session_id
from ..sdk_manager import get_backend_session_id, get_current_sdk_session_id


def load_handover_config() -> dict[str, Any]:
    """Load handover configuration from JSON file."""
    try:
        if os.path.exists(HANDOVER_CONFIG_PATH):
            with open(HANDOVER_CONFIG_PATH, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load handover config: {e}")

    return {"handovers": []}


@tool(
    "create_agent_task",
    "Create a task for another agent (direct handover) or for the user's inbox. "
    "Use with target_agent_id for direct handover to a configured agent. "
    "Use without target_agent_id to create an inbox task for user review.",
    {"task_message": str, "target_agent_id": str, "target_agent_name": str}
)
async def create_agent_task(args: dict[str, Any]) -> dict[str, Any]:
    """
    Create a task for another agent or for the user's inbox.

    Args:
        args: Dictionary with:
            - task_message: The task description/message (required)
            - target_agent_id: UUID of the target agent (optional - if provided, direct handover)
            - target_agent_name: Name of the target agent (optional - required if target_agent_id provided)

    Returns:
        Tool response with success status or error message
    """
    task_message = args.get("task_message", "").strip()
    target_agent_id = args.get("target_agent_id", "").strip() or None
    target_agent_name = args.get("target_agent_name", "").strip() or None

    # Validate parameters
    if not task_message:
        return {
            "content": [{
                "type": "text",
                "text": "Error: task_message parameter is required"
            }],
            "is_error": True
        }

    # If target_agent_id is provided, validate it's in handover config
    if target_agent_id:
        if not target_agent_name:
            return {
                "content": [{
                    "type": "text",
                    "text": "Error: target_agent_name is required when target_agent_id is provided"
                }],
                "is_error": True
            }

        # Load handover config and verify this handover is configured
        config = load_handover_config()
        handovers = config.get("handovers", [])

        # Check if this agent ID is in the configured handovers
        configured_agent_ids = [h.get("id") for h in handovers]
        if target_agent_id not in configured_agent_ids:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: Handover to agent '{target_agent_name}' ({target_agent_id}) is not configured. Available handovers: {', '.join([h.get('name', 'Unknown') for h in handovers])}"
                }],
                "is_error": True
            }

    # Validate backend configuration
    if not BACKEND_URL:
        logger.error("BACKEND_URL not configured")
        return {
            "content": [{
                "type": "text",
                "text": "Error: Backend URL not configured. Cannot create task."
            }],
            "is_error": True
        }

    if not AGENT_AUTH_TOKEN:
        logger.error("AGENT_AUTH_TOKEN not configured")
        return {
            "content": [{
                "type": "text",
                "text": "Error: Authentication token not configured. Cannot create task."
            }],
            "is_error": True
        }

    try:
        # Get current SDK session ID from global variable
        sdk_session_id = get_current_sdk_session_id()

        # Get backend session ID from the mapping (will use current SDK session if not provided)
        source_session_id = get_backend_session_id()

        if target_agent_id:
            logger.info(f"Creating handover task from SDK session {sdk_session_id}, backend session {source_session_id} to agent {target_agent_id} ({target_agent_name})")
        else:
            logger.info(f"Creating inbox task from SDK session {sdk_session_id}, backend session {source_session_id}")

        # Validate backend session_id is available
        if not source_session_id:
            logger.error(f"Backend session ID not available for SDK session {sdk_session_id}")
            return {
                "content": [{
                    "type": "text",
                    "text": "Error: Backend session ID not available. Cannot create task."
                }],
                "is_error": True
            }

        # Prepare request to create task
        url = f"{BACKEND_URL}/api/v1/agents/tasks/create"
        headers = {
            "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "task_message": task_message,
            "source_session_id": source_session_id
        }

        # Add target agent info only if provided (direct handover mode)
        if target_agent_id:
            payload["target_agent_id"] = target_agent_id
            payload["target_agent_name"] = target_agent_name

        logger.debug(f"Making create task request to {url}")

        # Make request to backend
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                data = response.json()
                success = data.get("success", False)
                task_id = data.get("task_id")
                session_id = data.get("session_id")  # Only set for direct handover
                message = data.get("message")
                error = data.get("error")

                if success and task_id:
                    if target_agent_id:
                        logger.info(f"Handover task created: task {task_id}, session {session_id} for agent {target_agent_id}")
                        return {
                            "content": [{
                                "type": "text",
                                "text": message or f"Successfully handed over to agent '{target_agent_name}'. A task has been created and is being executed."
                            }]
                        }
                    else:
                        logger.info(f"Inbox task created: task {task_id}")
                        return {
                            "content": [{
                                "type": "text",
                                "text": message or "Task created in user's inbox. The user will review, select an agent, and execute when ready."
                            }]
                        }
                else:
                    logger.error(f"Task creation failed: {error}")
                    return {
                        "content": [{
                            "type": "text",
                            "text": f"Task creation failed: {error}"
                        }],
                        "is_error": True
                    }
            elif response.status_code == 401:
                logger.error("Authentication failed when creating task")
                return {
                    "content": [{
                        "type": "text",
                        "text": "Error: Authentication failed. Invalid authentication token."
                    }],
                    "is_error": True
                }
            else:
                logger.error(f"Task creation request failed with status {response.status_code}: {response.text}")
                return {
                    "content": [{
                        "type": "text",
                        "text": f"Error: Task creation request failed (HTTP {response.status_code}): {response.text}"
                    }],
                    "is_error": True
                }

    except httpx.TimeoutException:
        logger.error(f"Timeout creating task")
        return {
            "content": [{
                "type": "text",
                "text": "Error: Task creation request timed out. Please try again."
            }],
            "is_error": True
        }
    except httpx.RequestError as e:
        logger.error(f"Request error creating task: {e}")
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Failed to connect to backend: {str(e)}"
            }],
            "is_error": True
        }
    except Exception as e:
        logger.error(f"Unexpected error creating task: {e}", exc_info=True)
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Unexpected error: {str(e)}"
            }],
            "is_error": True
        }


# Backward compatibility alias
agent_handover = create_agent_task
