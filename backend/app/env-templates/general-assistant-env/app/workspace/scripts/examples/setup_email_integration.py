"""
Set up email integration for an agent using an existing mail server.

API endpoints:
  POST /api/v1/agents/{agent_id}/email-integration
  GET  /api/v1/agents/{agent_id}/email-integration

Prerequisites:
  - A mail server must already exist (use list_mail_servers.py to find one)
  - The agent must have an active environment
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post, api_get


def get_email_integration(agent_id: str) -> dict | None:
    """Get existing email integration for an agent, or None if not configured."""
    try:
        return api_get(f"/api/v1/agents/{agent_id}/email-integration")
    except Exception:
        return None


def setup_email_integration(
    agent_id: str,
    mail_server_id: str,
    enabled: bool = True,
    process_interval: int = 60,
    email_subject_pattern: str | None = None,
) -> dict:
    """Configure email integration for an agent."""
    payload: dict = {
        "mail_server_id": mail_server_id,
        "enabled": enabled,
        "process_interval": process_interval,
        "email_subject_pattern": email_subject_pattern,
    }
    result = api_post(f"/api/v1/agents/{agent_id}/email-integration", json=payload)
    return result


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python setup_email_integration.py <agent_id> <mail_server_id> [process_interval] [subject_pattern]")
        print("Example: python setup_email_integration.py 'agent-uuid' 'server-uuid' 60")
        print()
        print("Use list_mail_servers.py to find available mail server IDs.")
        sys.exit(1)

    agent_id = sys.argv[1]
    mail_server_id = sys.argv[2]
    process_interval = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    email_subject_pattern = sys.argv[4] if len(sys.argv) > 4 else None

    # Check if integration already exists
    existing = get_email_integration(agent_id)
    if existing:
        print(f"Agent already has email integration configured (ID: {existing['id']}).")
        print("Updating configuration...")

    print(f"Setting up email integration for agent {agent_id}...")
    print(f"  Mail server:      {mail_server_id}")
    print(f"  Process interval: {process_interval}s")
    print(f"  Subject pattern:  {email_subject_pattern or '(all emails)'}")

    result = setup_email_integration(
        agent_id=agent_id,
        mail_server_id=mail_server_id,
        process_interval=process_interval,
        email_subject_pattern=email_subject_pattern,
    )

    print(f"\nEmail integration configured successfully!")
    print(f"  ID:      {result['id']}")
    print(f"  Enabled: {result['enabled']}")


if __name__ == "__main__":
    main()
