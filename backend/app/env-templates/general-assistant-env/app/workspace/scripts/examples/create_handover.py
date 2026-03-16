"""
Create a handover configuration between two agents.

This allows the source agent to delegate tasks to the target agent.

API endpoints:
  POST /api/v1/agents/{agent_id}/handovers/generate  (AI prompt generation)
  POST /api/v1/agents/{agent_id}/handovers/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post, api_get


def generate_handover_prompt(source_agent_id: str, target_agent_id: str) -> str:
    """Use AI to generate a handover prompt based on both agents' descriptions."""
    result = api_post(
        f"/api/v1/agents/{source_agent_id}/handovers/generate",
        json={"target_agent_id": target_agent_id},
    )
    return result["handover_prompt"]


def create_handover(source_agent_id: str, target_agent_id: str, handover_prompt: str) -> dict:
    """Create a handover configuration from source to target agent."""
    handover = api_post(
        f"/api/v1/agents/{source_agent_id}/handovers/",
        json={
            "target_agent_id": target_agent_id,
            "handover_prompt": handover_prompt,
        },
    )
    return handover


def get_agent_name(agent_id: str) -> str:
    """Fetch an agent's name for display purposes."""
    try:
        agent = api_get(f"/api/v1/agents/{agent_id}")
        return agent.get("name", agent_id)
    except Exception:
        return agent_id


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python create_handover.py <source_agent_id> <target_agent_id> [handover_prompt]")
        print()
        print("If handover_prompt is omitted, AI will generate one automatically.")
        print()
        print("Example:")
        print("  python create_handover.py 'coordinator-uuid' 'analyst-uuid'")
        sys.exit(1)

    source_agent_id = sys.argv[1]
    target_agent_id = sys.argv[2]
    handover_prompt = sys.argv[3] if len(sys.argv) > 3 else None

    source_name = get_agent_name(source_agent_id)
    target_name = get_agent_name(target_agent_id)

    print(f"Setting up handover from '{source_name}' → '{target_name}'")

    if not handover_prompt:
        print("Generating handover prompt using AI...")
        handover_prompt = generate_handover_prompt(
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
        )
        print(f"Generated prompt:\n  {handover_prompt[:200]}...")

    print("\nCreating handover configuration...")
    handover = create_handover(
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        handover_prompt=handover_prompt,
    )

    print(f"\nHandover configured successfully!")
    print(f"  ID:      {handover['id']}")
    print(f"  Source:  {source_name}")
    print(f"  Target:  {target_name}")
    print(f"  Enabled: {handover['enabled']}")
    print()
    print(f"'{source_name}' can now delegate tasks to '{target_name}'.")


if __name__ == "__main__":
    main()
