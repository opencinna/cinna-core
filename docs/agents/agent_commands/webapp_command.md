# /webapp Command

## Purpose

Returns the shareable webapp URL for the current agent. Provides a quick way for users (and A2A clients) to get the public webapp link without navigating to the Integrations tab.

## Behavior

1. Checks if `webapp_enabled` is `True` on the agent
2. If disabled, returns: "No Web App available for this agent."
3. If enabled, queries for the first active, non-expired `AgentWebappShare` (ordered by creation date ascending)
4. If a valid share is found, returns a clickable markdown link to the share URL
5. If the share has a security code, appends the access code to the response
6. If no valid share exists, returns: "No Web App available for this agent."

## Response Examples

**Webapp available (no security code):**
```
**Web App:** [https://app.example.com/webapp/abc123](https://app.example.com/webapp/abc123)
```

**Webapp available (with security code):**
```
**Web App:** [https://app.example.com/webapp/abc123](https://app.example.com/webapp/abc123)
**Access Code:** `1234`
```

**No webapp:**
```
No Web App available for this agent.
```

## Technical Details

- **Handler**: `WebappCommandHandler` in `backend/app/services/agents/commands/webapp_command.py`
- **Share lookup**: Delegates to `AgentWebappShareService.get_first_active_share_url()` in `backend/app/services/webapp/agent_webapp_share_service.py`
- **No environment required**: Unlike `/files`, this command only queries the database (agent + shares), so it works even if the environment is not running
- **Share selection**: Picks the oldest active, non-expired share — consistent and predictable for users with multiple shares
- **Link format**: Always uses `{FRONTEND_HOST}/webapp/{token}` regardless of caller context (UI or A2A), since the webapp is a standalone page

## Integration Points

- **[Agent Webapp](../agent_webapp/agent_webapp.md)** — reads `webapp_enabled` flag and `AgentWebappShare` records
- **[Agent Commands](agent_commands.md)** — registered as a standard command handler in the command framework

---

*Last updated: 2026-03-07*
