"""
List all agents accessible to the current user.

API endpoint: GET /api/v1/agents/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_get


def list_agents(workspace_id: str | None = None, limit: int = 200) -> list[dict]:
    """Fetch and return all agents, optionally filtered by workspace."""
    params: dict = {"limit": limit}
    if workspace_id:
        params["user_workspace_id"] = workspace_id

    result = api_get("/api/v1/agents/", params=params)
    return result.get("data", [])


def main() -> None:
    workspace_id = sys.argv[1] if len(sys.argv) > 1 else None

    agents = list_agents(workspace_id=workspace_id)

    if not agents:
        print("No agents found.")
        return

    print(f"Found {len(agents)} agent(s):\n")
    for agent in agents:
        workspace = agent.get("user_workspace_id") or "no workspace"
        print(f"  ID:          {agent['id']}")
        print(f"  Name:        {agent['name']}")
        print(f"  Description: {agent.get('description', '')[:80]}")
        print(f"  Workspace:   {workspace}")
        print(f"  Color:       {agent.get('ui_color_preset', 'default')}")
        print()


if __name__ == "__main__":
    main()
