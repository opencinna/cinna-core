"""
Update an agent's prompts and sync them to the running environment.

API endpoints:
  PUT  /api/v1/agents/{agent_id}
  POST /api/v1/agents/{agent_id}/sync-prompts
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_put, api_post


def update_agent_prompts(
    agent_id: str,
    workflow_prompt: str | None = None,
    entrypoint_prompt: str | None = None,
    refiner_prompt: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update agent prompts and return the updated agent."""
    payload: dict = {}
    if workflow_prompt is not None:
        payload["workflow_prompt"] = workflow_prompt
    if entrypoint_prompt is not None:
        payload["entrypoint_prompt"] = entrypoint_prompt
    if refiner_prompt is not None:
        payload["refiner_prompt"] = refiner_prompt
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description

    if not payload:
        raise ValueError("At least one field must be provided to update.")

    agent = api_put(f"/api/v1/agents/{agent_id}", json=payload)
    return agent


def sync_prompts(agent_id: str) -> dict:
    """Push updated prompts to the running environment."""
    result = api_post(f"/api/v1/agents/{agent_id}/sync-prompts")
    return result


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python update_agent_prompts.py <agent_id> <workflow_prompt>")
        print("Example: python update_agent_prompts.py 'agent-uuid' 'You are a helpful assistant...'")
        sys.exit(1)

    agent_id = sys.argv[1]
    workflow_prompt = sys.argv[2]

    print(f"Updating prompts for agent {agent_id}...")
    agent = update_agent_prompts(agent_id=agent_id, workflow_prompt=workflow_prompt)
    print(f"  Agent updated: {agent['name']}")

    print("Syncing prompts to environment...")
    sync_result = sync_prompts(agent_id=agent_id)
    print(f"  {sync_result.get('message', 'Done')}")


if __name__ == "__main__":
    main()
