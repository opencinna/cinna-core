# Example Scripts

Working code patterns for interacting with the platform API.
These scripts use `platform_helper.py` for authenticated requests.

## Available Examples

| Script | Purpose | API Endpoints Used |
|--------|---------|-------------------|
| platform_helper.py | Shared auth & request utilities | - |
| list_agents.py | List all agents | GET /agents/ |
| create_workspace.py | Create a workspace | POST /workspaces/ |
| list_workspaces.py | List workspaces | GET /workspaces/ |
| create_agent.py | Create a new agent | POST /agents/ |
| update_agent_prompts.py | Update agent prompts and sync | PUT /agents/{id}, POST /agents/{id}/sync-prompts |
| create_session_and_send_message.py | Create session for an agent | POST /sessions/ |
| list_credentials.py | List available credentials | GET /credentials/ |
| link_credential_to_agent.py | Link credential to agent | POST /agents/{id}/credentials |
| setup_email_integration.py | Set up email automation | POST /agents/{id}/email-integration |
| list_mail_servers.py | List mail servers | GET /mail-servers/ |
| create_scheduler.py | Create CRON schedule | POST /agents/{id}/schedules/ |
| create_handover.py | Set up agent handover | POST /agents/{id}/handovers/ |

## Usage Pattern

```python
# Import the helper
from platform_helper import api_get, api_post

# List agents
agents = api_get("/api/v1/agents/")
print(f"Found {agents['count']} agents")

# Create a workspace
workspace = api_post("/api/v1/workspaces/", json={"name": "My Workspace", "icon": "folder-kanban"})
print(f"Created: {workspace['id']}")
```

## Running Scripts

Scripts can be run directly from the workspace:
```bash
cd /app/workspace
python scripts/examples/list_agents.py
python scripts/examples/create_agent.py "My Agent" "Agent description" "workspace-uuid"
```

Or adapted and run inline during a building session.
