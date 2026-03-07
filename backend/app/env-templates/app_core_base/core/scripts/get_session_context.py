#!/usr/bin/env python3
"""Get session context from agent-env core server.

Usage:
    # As CLI:
    python /app/core/scripts/get_session_context.py <session_id>

    # As import:
    from core.scripts.get_session_context import get_session_context
    ctx = get_session_context("abc123-def456")

The session_id is provided to the LLM in the system prompt under
"Session Context (Server-Verified, Read-Only)". The LLM passes it
to scripts as a CLI argument.

Returns HMAC-verified session context from the core server's
in-memory store. This is the authoritative source for session
metadata (sender email, subject, integration type, etc.).
"""
import json
import os
import sys
import urllib.request
import urllib.error


def get_session_context(session_id: str) -> dict:
    """Get verified session context by backend session ID.

    Args:
        session_id: Backend session ID (UUID string from system prompt)

    Returns:
        Session context dict with keys like integration_type, sender_email,
        email_subject, agent_id, is_clone, parent_agent_id, etc.

    Raises:
        urllib.error.HTTPError: If session_id not found (404) or server error
        urllib.error.URLError: If core server is unreachable
    """
    port = os.getenv("AGENT_PORT", "8000")
    url = f"http://localhost:{port}/session/context?session_id={session_id}"
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: get_session_context.py <session_id>", file=sys.stderr)
        sys.exit(1)

    try:
        ctx = get_session_context(sys.argv[1])
        print(json.dumps(ctx, indent=2))
    except urllib.error.HTTPError as e:
        print(f"Error: {e.code} - {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Cannot connect to core server - {e.reason}", file=sys.stderr)
        sys.exit(1)
