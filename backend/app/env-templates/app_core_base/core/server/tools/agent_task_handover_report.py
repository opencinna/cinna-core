"""
Agent Task Handover Report Tool for Agent Environment.

Lets an agent that was delegated a task by another agent send the structured
result the requester is waiting for: in_progress, blocked (with a question),
done or failed. The requester reads this report, not the chat transcript, so a
delegated task that only answers in chat leaves the requester waiting forever.

Mirrors ``handover_report`` in the OpenCode stdio bridge
(``mcp_bridge/task_server.py``); both post to
``/api/v1/agent/tasks/current/delegation-result``.
"""
import json
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

REPORT_STATUSES = frozenset(["in_progress", "blocked", "done", "failed"])
AUDIENCES = frozenset(["requester", "user"])
# Must match DelegationReport.audience's default on the backend.
DEFAULT_AUDIENCE = "user"


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


@tool(
    "handover_report",
    "Report this delegated task's result to the agent that requested it: status "
    "in_progress, blocked, done or failed. 'blocked' requires a 'question'; use "
    "audience 'requester' when the requesting agent can answer, 'user' (default) when "
    "only a person can. Artifacts need portable HTTP(S) URLs — upload workspace files "
    "with add_comment first. End your turn after reporting: the requester receives a "
    "durable result and its reply resumes this conversation.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "in_progress / blocked / done / failed (required)"},
            "summary": {"type": "string", "description": "One-line summary, max 1000 chars (required)"},
            "question": {"type": "string", "description": "Question to answer; required when status is blocked"},
            "audience": {"type": "string", "description": "Who should answer a blocked question: requester or user (default user)"},
            "artifacts": {
                "type": "array",
                "description": "Portable outputs: objects with kind (file|link), name, ref (HTTP(S) URL)",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "name": {"type": "string"},
                        "ref": {"type": "string"},
                    },
                    "required": ["kind", "name", "ref"],
                },
            },
            "body": {"type": "string", "description": "The full answer or report body"},
        },
        "required": ["status", "summary"],
    },
)
async def agent_task_handover_report(args: dict[str, Any]) -> dict[str, Any]:
    """Post a structured delegation report for the current task session."""
    status = (args.get("status") or "").strip()
    summary = (args.get("summary") or "").strip()
    question = (args.get("question") or "").strip() or None
    audience = (args.get("audience") or "").strip() or DEFAULT_AUDIENCE
    artifacts = args.get("artifacts") or []
    body = args.get("body") or ""

    if status not in REPORT_STATUSES:
        return _error(f"status must be one of: {', '.join(sorted(REPORT_STATUSES))}")
    if not summary:
        return _error("summary is required")
    if status == "blocked" and not question:
        return _error("a blocked report must contain a question")
    if audience not in AUDIENCES:
        return _error(f"audience must be one of: {', '.join(sorted(AUDIENCES))}")
    if not BACKEND_URL:
        return _error("Backend URL not configured")
    if not AGENT_AUTH_TOKEN:
        return _error("Authentication token not configured")

    source_session_id = get_backend_session_id()
    if not source_session_id:
        return _error("Backend session ID not available")

    headers = {
        "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
        "X-Agent-Env-Id": ENV_ID,
        "Content-Type": "application/json",
    }
    payload = {
        "source_session_id": source_session_id,
        "status": status,
        "summary": summary,
        "question": question,
        "audience": audience,
        "artifacts": artifacts,
        "body": body,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BACKEND_URL}/api/v1/agent/tasks/current/delegation-result",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException:
        return _error("Report acknowledgement timed out; delivery is uncertain")
    except httpx.RequestError as exc:
        logger.error(f"Request error in agent_task_handover_report: {exc}")
        return _error(f"Failed to connect to backend: {exc}")

    if response.status_code == 200:
        logger.info(f"Delegation report accepted: status={status}")
        return {"content": [{"type": "text", "text": json.dumps(response.json())}]}
    if response.status_code == 401:
        return _error("Authentication failed")
    logger.error(f"handover_report failed HTTP {response.status_code}: {response.text}")
    return _error(f"Report was not accepted (HTTP {response.status_code}): {response.text}")
