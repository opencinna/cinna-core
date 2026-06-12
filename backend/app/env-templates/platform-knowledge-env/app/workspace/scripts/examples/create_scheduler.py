"""
Create a CRON schedule for an agent.

API endpoints:
  POST /api/v1/agents/{agent_id}/schedules/generate  (natural language → CRON)
  POST /api/v1/agents/{agent_id}/schedules/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post


def generate_cron(agent_id: str, natural_language: str, timezone: str = "UTC") -> dict:
    """Convert natural language schedule description to CRON string."""
    result = api_post(
        f"/api/v1/agents/{agent_id}/schedules/generate",
        json={"natural_language": natural_language, "timezone": timezone},
    )
    return result


def create_schedule(
    agent_id: str,
    name: str,
    cron_string: str,
    timezone: str,
    prompt: str,
    description: str = "",
    enabled: bool = True,
) -> dict:
    """Create a CRON schedule for an agent."""
    payload: dict = {
        "name": name,
        "cron_string": cron_string,
        "timezone": timezone,
        "description": description,
        "prompt": prompt,
        "enabled": enabled,
    }
    schedule = api_post(f"/api/v1/agents/{agent_id}/schedules/", json=payload)
    return schedule


def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: python create_scheduler.py <agent_id> <name> <natural_language> <prompt> [timezone]")
        print()
        print("Example:")
        print("  python create_scheduler.py 'agent-uuid' 'Daily Report' \\")
        print("    'Every weekday at 9am' \\")
        print("    'Generate the daily sales report and send it to the team.' \\")
        print("    'Europe/London'")
        sys.exit(1)

    agent_id = sys.argv[1]
    name = sys.argv[2]
    natural_language = sys.argv[3]
    prompt = sys.argv[4]
    timezone = sys.argv[5] if len(sys.argv) > 5 else "UTC"

    print(f"Generating CRON expression for: '{natural_language}'...")
    cron_result = generate_cron(agent_id=agent_id, natural_language=natural_language, timezone=timezone)
    cron_string = cron_result["cron_string"]
    description = cron_result.get("description", "")
    print(f"  CRON: {cron_string}")
    print(f"  Desc: {description}")

    print(f"\nCreating schedule '{name}'...")
    schedule = create_schedule(
        agent_id=agent_id,
        name=name,
        cron_string=cron_string,
        timezone=timezone,
        prompt=prompt,
        description=description,
    )

    print(f"\nSchedule created successfully!")
    print(f"  ID:       {schedule['id']}")
    print(f"  Name:     {schedule['name']}")
    print(f"  CRON:     {schedule['cron_string']}")
    print(f"  Timezone: {schedule['timezone']}")
    print(f"  Enabled:  {schedule['enabled']}")


if __name__ == "__main__":
    main()
