"""
Create a new agent.

API endpoint: POST /api/v1/agents/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post


def create_agent(
    name: str,
    description: str = "",
    workspace_id: str | None = None,
    workflow_prompt: str = "",
    entrypoint_prompt: str = "",
    ui_color_preset: str = "blue",
    agent_sdk_building: str = "claude-code/anthropic",
    agent_sdk_conversation: str = "claude-code/anthropic",
) -> dict:
    """Create an agent and return the created object."""
    payload: dict = {
        "name": name,
        "description": description,
        "workflow_prompt": workflow_prompt,
        "entrypoint_prompt": entrypoint_prompt,
        "refiner_prompt": "",
        "ui_color_preset": ui_color_preset,
        "user_workspace_id": workspace_id,
        "agent_sdk_building": agent_sdk_building,
        "agent_sdk_conversation": agent_sdk_conversation,
    }
    agent = api_post("/api/v1/agents/", json=payload)
    return agent


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python create_agent.py <name> [description] [workspace_id]")
        print("Example: python create_agent.py 'My Agent' 'Does helpful things' 'ws-uuid'")
        sys.exit(1)

    name = sys.argv[1]
    description = sys.argv[2] if len(sys.argv) > 2 else ""
    workspace_id = sys.argv[3] if len(sys.argv) > 3 else None

    print(f"Creating agent '{name}'...")
    agent = create_agent(name=name, description=description, workspace_id=workspace_id)

    print(f"\nAgent created successfully!")
    print(f"  ID:          {agent['id']}")
    print(f"  Name:        {agent['name']}")
    print(f"  Description: {agent.get('description', '')}")
    print(f"  Workspace:   {agent.get('user_workspace_id') or 'none'}")
    print()
    print("Next steps:")
    print("  1. Update prompts using update_agent_prompts.py")
    print("  2. Create an environment for the agent")
    print("  3. Create a building session to configure the agent")


if __name__ == "__main__":
    main()
