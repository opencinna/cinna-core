"""
Link a credential to an agent so it can be used in that agent's environment.

API endpoint: POST /api/v1/agents/{agent_id}/credentials
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post, api_get


def link_credential_to_agent(agent_id: str, credential_id: str) -> dict:
    """Link a credential to an agent and return the result."""
    result = api_post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={"credential_id": credential_id},
    )
    return result


def list_agent_credentials(agent_id: str) -> list[dict]:
    """List credentials currently linked to an agent."""
    result = api_get(f"/api/v1/agents/{agent_id}/credentials")
    return result.get("data", [])


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python link_credential_to_agent.py <agent_id> <credential_id>")
        print("Example: python link_credential_to_agent.py 'agent-uuid' 'cred-uuid'")
        print()
        print("Use list_agents.py and list_credentials.py to find the right IDs.")
        sys.exit(1)

    agent_id = sys.argv[1]
    credential_id = sys.argv[2]

    print(f"Linking credential {credential_id} to agent {agent_id}...")
    link_credential_to_agent(agent_id=agent_id, credential_id=credential_id)
    print("Credential linked successfully.")

    print("\nCredentials now linked to this agent:")
    credentials = list_agent_credentials(agent_id=agent_id)
    for cred in credentials:
        print(f"  - {cred['name']} ({cred['type']}) — {cred['id']}")


if __name__ == "__main__":
    main()
