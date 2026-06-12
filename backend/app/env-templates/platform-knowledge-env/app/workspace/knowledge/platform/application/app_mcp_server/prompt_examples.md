# Prompt Examples

## Purpose

Prompt examples give MCP client users ready-to-use task suggestions for each agent route. Instead of guessing what an agent can do, the MCP client's `prompts/list` response includes short, actionable prompts that can be sent directly — no routing prefix ("ask cinna to...") needed.

This feature applies to both App MCP Server routes (AppAgentRoute) and Identity MCP Server bindings (IdentityAgentBinding), with different presentation behavior for each.

## Core Concepts

| Term | Definition |
|------|-----------|
| **Prompt Examples** | Optional newline-separated text field on a route or binding containing short task suggestions (one per line) |
| **MCP Prompt** | An entry in the MCP `prompts/list` response that MCP clients (Claude Desktop, Cursor) display as a suggested action |
| **Prefixing** | For identity bindings, each example is automatically wrapped with "ask {Owner Name} ({email}) to {example}" so callers address the right person |

## How It Works

### App MCP Server Routes

Route owners write short task descriptions in a textarea (one per line). Each non-empty line becomes an individual MCP prompt returned via `prompts/list`, alongside the existing trigger-prompt-based prompt.

Examples are returned **as-is** — they already skip the routing prefix, so MCP clients can send them directly to the `send_message` tool.

```
Route prompt_examples:
  "generate employee report"
  "summarize last quarter sales"

MCP client prompts/list sees:
  "generate employee report"
  "summarize last quarter sales"
```

### Identity MCP Server Bindings

Identity binding owners write the same short task descriptions. When building effective routes for a caller, the system aggregates examples from all active bindings accessible to that caller and **prefixes each line** with the identity owner's name and email.

This is necessary because identity messages go through two-stage routing — the caller must address the person first, then the identity router selects the agent.

```
Binding prompt_examples (owner = John Doe, john@example.com):
  "generate employee report"
  "summarize last quarter sales"

Caller's MCP client prompts/list sees:
  "ask John Doe (john@example.com) to generate employee report"
  "ask John Doe (john@example.com) to summarize last quarter sales"
```

If a caller has access to multiple bindings from the same owner, all examples are aggregated into a single combined list under that owner's identity route.

### Validation Rules

- Maximum **2000 characters** total per field
- Maximum **10 non-empty lines** per route or binding
- Empty lines and whitespace-only lines are ignored (not counted)
- Validation runs on both create and update (POST and PUT)
- Violations return HTTP 422

### Visibility Rules

- Routes without `prompt_examples` behave exactly as before (only the trigger-prompt-based prompt is emitted)
- For identity bindings, only examples from bindings where the caller has an active, enabled assignment are included
- The field is optional and defaults to null

## User Flows

### Setting Prompt Examples on an App MCP Route

1. Agent owner opens Integrations tab → MCP Connectors card
2. Creates or edits an App MCP Server Integration
3. Fills in the "Prompt Examples" textarea — one example per line
4. Saves — examples are stored on the route
5. Connected MCP clients see the examples in their prompt suggestions

### Setting Prompt Examples on an Identity Binding

1. Identity owner opens Integrations tab → MCP Connectors card → "Identity MCP Server Integration"
2. Fills in the "Prompt Examples" textarea
3. Saves — examples are stored on the binding
4. Callers with active assignments see the prefixed examples in their MCP client

Alternative path: Settings → Channels tab → Identity Server card → edit binding → "Prompt Examples" textarea

## Integration Points

- **[App MCP Server](app_mcp_server.md)** — prompt examples are a field on `AppAgentRoute`; emitted in `prompts/list` via `app_prompts.py`
- **[Identity MCP Server](../identity_mcp_server/identity_mcp_server.md)** — prompt examples on `IdentityAgentBinding` are aggregated and prefixed when building effective routes for a caller
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — examples appear as MCP prompts in the standard `prompts/list` protocol response
