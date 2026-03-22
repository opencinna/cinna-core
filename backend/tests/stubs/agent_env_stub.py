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
