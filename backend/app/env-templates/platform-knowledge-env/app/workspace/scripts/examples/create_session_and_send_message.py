"""
Create a building session for an agent.

Note: Sending messages to a session is done through the platform UI or via
the agent environment's WebSocket. This script creates the session so it
appears in the UI ready for interaction.

API endpoint: POST /api/v1/sessions/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post


def create_session(agent_id: str, mode: str = "building", title: str = "") -> dict:
    """Create a session for an agent and return the session object."""
    payload: dict = {
        "agent_id": agent_id,
        "mode": mode,
    }
    if title:
        payload["title"] = title

    session = api_post("/api/v1/sessions/", json=payload)
    return session


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python create_session_and_send_message.py <agent_id> [mode] [title]")
        print("Example: python create_session_and_send_message.py 'agent-uuid' building 'Setup session'")
        print()
        print("Mode options: building (default), conversation")
        sys.exit(1)

    agent_id = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "building"
    title = sys.argv[3] if len(sys.argv) > 3 else ""

    print(f"Creating {mode} session for agent {agent_id}...")
    session = create_session(agent_id=agent_id, mode=mode, title=title)

    print(f"\nSession created successfully!")
    print(f"  ID:     {session['id']}")
    print(f"  Mode:   {session['mode']}")
    print(f"  Title:  {session.get('title', 'untitled')}")
    print(f"  Status: {session.get('status', 'unknown')}")
    print()
    print("Open this session in the platform UI to start interacting.")


if __name__ == "__main__":
    main()
