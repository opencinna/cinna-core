"""
Agent Task Add Comment Tool for Agent Environment.

Allows an agent to post a comment on a task — the primary way agents report
findings, results, and progress. Optionally attaches workspace files to the comment.
"""
import logging
import os
from pathlib import Path
from typing import Any
import httpx

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")

from ..sdk_manager import get_backend_session_id


@tool(
    "add_comment",
    "Post a comment on a task to report findings, results, or progress. "
    "The primary way to share work with the user and other agents. "
    "Optionally attach workspace files to the comment. "
    "Defaults to the current task if 'task' is not specified.",
    {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Comment text (required, markdown supported)"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Workspace file paths to attach (optional)"},
            "task": {"type": "string", "description": "Short code of target task (optional, defaults to current task)"},
        },
        "required": ["content"],
    },
)
async def agent_task_add_comment(args: dict[str, Any]) -> dict[str, Any]:
    """
    Post a comment on a task.

    Args:
        args: Dictionary with:
            - content: Comment text (required, markdown supported)
            - files: List of workspace file paths to attach (optional)
            - task: Short code of target task (optional, defaults to current task)

    Returns:
        Tool response with comment_id, task short_code, and attachments_count.
    """
    content = args.get("content", "").strip()
    files: list[str] = args.get("files") or []
    task_short_code: str | None = args.get("task", "").strip() or None

    if not content:
        return {
            "content": [{"type": "text", "text": "Error: content is required"}],
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

    # Validate file paths exist locally before sending to backend
    if files:
        workspace = Path("/app/workspace")
        missing = []
        resolved_files = []
        for f in files:
            fp = Path(f)
            # Resolve relative paths against workspace
            if not fp.is_absolute():
                fp = workspace / fp
            if not fp.exists():
                missing.append(f)
            else:
                resolved_files.append(str(fp))
        if missing:
            missing_list = "\n".join(f"  - {m}" for m in missing)
            return {
                "content": [{"type": "text", "text": f"Error: The following files were not found in the workspace:\n{missing_list}\n\nFix the file paths and try again. Comment was NOT posted."}],
                "is_error": True,
            }
        files = resolved_files

    headers = {
        "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            task_id: str | None = None

            if task_short_code:
                # Resolve short_code to task_id
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

            # Post comment — backend resolves task from session if task_id is None
            url_path = f"/api/v1/agent/tasks/{task_id}/comment" if task_id else "/api/v1/agent/tasks/current/comment"
            payload: dict[str, Any] = {
                "content": content,
                "source_session_id": source_session_id,
            }
            if files:
                payload["file_paths"] = files

            comment_resp = await client.post(
                f"{BACKEND_URL}{url_path}",
                json=payload,
                headers=headers,
            )

        if comment_resp.status_code == 200:
            data = comment_resp.json()
            comment_id = data.get("comment_id")
            result_task = data.get("task", task_short_code or "")
            attachments_count = data.get("attachments_count", 0)
            failed_attachments = data.get("failed_attachments", 0)
            logger.info(f"Comment posted on task {result_task}: comment_id={comment_id}")
            parts = [f"Comment posted on task {result_task} (comment_id: {comment_id})."]
            if attachments_count:
                parts.append(f"Attached {attachments_count} file(s).")
            if failed_attachments:
                detail = data.get("message", f"{failed_attachments} file(s) could not be attached.")
                prefix = "Error" if attachments_count == 0 else "WARNING"
                return {
                    "content": [{"type": "text", "text": f"{prefix}: {detail}\n\n" + " ".join(parts)}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": " ".join(parts)}]}

        if comment_resp.status_code == 401:
            return {
                "content": [{"type": "text", "text": "Error: Authentication failed"}],
                "is_error": True,
            }
        if comment_resp.status_code == 404:
            return {
                "content": [{"type": "text", "text": "Error: Task not found or not accessible"}],
                "is_error": True,
            }

        logger.error(f"add_comment failed HTTP {comment_resp.status_code}: {comment_resp.text}")
        return {
            "content": [{
                "type": "text",
                "text": f"Error: Request failed (HTTP {comment_resp.status_code}): {comment_resp.text}",
            }],
            "is_error": True,
        }

    except httpx.TimeoutException:
        return {
            "content": [{"type": "text", "text": "Error: Request timed out"}],
            "is_error": True,
        }
    except httpx.RequestError as exc:
        logger.error(f"Request error in agent_task_add_comment: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error: Failed to connect to backend: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        logger.error(f"Unexpected error in agent_task_add_comment: {exc}", exc_info=True)
        return {
            "content": [{"type": "text", "text": f"Error: Unexpected error: {exc}"}],
            "is_error": True,
        }
