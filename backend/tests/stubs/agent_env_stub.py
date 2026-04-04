"""
Agent-env streaming stub matching the AgentEnvConnector interface.

StubAgentEnvConnector returns preconfigured SSE events and tracks calls.
Follows the same pattern as StubSMTPConnector / StubIMAPConnector.
"""
import uuid


def build_simple_response_events(
    text: str,
    session_id: str | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> list[dict]:
    """Build a minimal successful SSE event sequence from plain text."""
    sid = session_id or str(uuid.uuid4())
    return [
        {"type": "session_created", "content": "", "session_id": sid, "metadata": {}},
        {
            "type": "system",
            "subtype": "tools_init",
            "content": "",
            "data": {"tools": ["bash", "read", "write", "edit", "glob", "grep"]},
            "metadata": {},
        },
        {"type": "assistant", "content": text, "metadata": {"model": model}},
        {"type": "done"},
    ]


class StubAgentEnvConnector:
    """Returns predefined SSE events. Tracks stream_chat calls."""

    def __init__(
        self,
        events: list[dict] | None = None,
        response_text: str | None = None,
    ):
        if response_text:
            self.events = build_simple_response_events(response_text)
        else:
            self.events = events or []
        self.stream_calls: list[dict] = []

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append({"base_url": base_url, "payload": payload})
        for event in self.events:
            yield event


class ScriptedAgentEnvConnector:
    """Agent-env stub that executes scripted MCP tool calls during streaming.

    Simulates real agent behavior: the SDK processes a message, calls MCP tools
    (which make HTTP requests back to the backend), and then completes the stream.

    Usage::

        stub = ScriptedAgentEnvConnector(
            client=client,
            auth_headers=superuser_token_headers,
            steps=[
                {"type": "assistant", "content": "I'll delegate this to the recruiting team."},
                {
                    "type": "tool_call",
                    "endpoint": "/api/v1/agent/tasks/current/subtask",
                    "method": "POST",
                    "json": {"title": "Find candidates", "assigned_to": "Recruiter", ...},
                    "tool_name": "mcp__agent_task__create_subtask",
                },
                {"type": "assistant", "content": "Subtask delegated. Waiting for results."},
            ],
        )

    Each step can be:
    - ``{"type": "assistant", "content": "..."}`` — yields an assistant SSE event
    - ``{"type": "tool_call", ...}`` — makes an HTTP call to the backend (simulating
      an MCP tool inside the agent-env), then yields a tool SSE event with the result
    - Any other dict — yielded as-is (raw SSE event)

    The connector automatically prepends ``session_created`` and ``tools_init``
    events and appends a ``done`` event, matching the real agent-env protocol.

    **Re-entry behavior:** Only the first ``stream_chat`` call executes the
    scripted steps. Subsequent calls (e.g., feedback delivery re-triggering
    the lead agent's session) yield a simple fallback response. This prevents
    infinite cascading during ``drain_tasks()``. Use ``fallback_call_count``
    to verify how many times the fallback path was taken.
    """

    def __init__(
        self,
        client,  # FastAPI TestClient
        auth_headers: dict[str, str],
        steps: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        fallback_text: str = "Acknowledged.",
    ):
        self.client = client
        self.auth_headers = auth_headers
        self.steps = steps
        self.model = model
        self.fallback_text = fallback_text
        self.stream_calls: list[dict] = []
        self.tool_results: list[dict] = []
        self.fallback_call_count: int = 0

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append({"base_url": base_url, "payload": payload})
        is_first_call = len(self.stream_calls) == 1

        sid = str(uuid.uuid4())
        yield {"type": "session_created", "content": "", "session_id": sid, "metadata": {}}
        yield {
            "type": "system",
            "subtype": "tools_init",
            "content": "",
            "data": {"tools": ["bash", "read", "write", "edit", "glob", "grep"]},
            "metadata": {},
        }

        if is_first_call:
            for step in self.steps:
                step_type = step.get("type", "")

                if step_type == "tool_call":
                    endpoint = step["endpoint"]
                    method = step.get("method", "POST").upper()
                    json_body = step.get("json")
                    params = step.get("params")
                    tool_name = step.get("tool_name", "unknown_tool")

                    if method == "POST":
                        r = self.client.post(endpoint, headers=self.auth_headers, json=json_body)
                    elif method == "GET":
                        r = self.client.get(endpoint, headers=self.auth_headers, params=params)
                    else:
                        raise ValueError(f"Unsupported method: {method}")

                    result = {
                        "endpoint": endpoint,
                        "status_code": r.status_code,
                        "body": r.json() if r.status_code == 200 else r.text,
                    }
                    self.tool_results.append(result)

                    yield {
                        "type": "tool",
                        "tool_name": tool_name,
                        "content": str(result["body"]),
                        "metadata": {"tool_input": json_body or params or {}},
                    }

                elif step_type == "assistant":
                    yield {
                        "type": "assistant",
                        "content": step.get("content", ""),
                        "metadata": {"model": self.model},
                    }

                else:
                    yield step
        else:
            self.fallback_call_count += 1
            yield {
                "type": "assistant",
                "content": self.fallback_text,
                "metadata": {"model": self.model},
            }

        yield {"type": "done"}
