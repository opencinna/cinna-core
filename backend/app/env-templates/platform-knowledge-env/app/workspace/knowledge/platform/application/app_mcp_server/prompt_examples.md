# Prompt Examples

## Purpose

Prompt examples give MCP client users ready-to-use task suggestions for each addressable target. Instead of guessing what an agent can do, the MCP client's `prompts/list` response includes short, actionable prompts that can be sent directly — no routing prefix ("ask cinna to...") needed.

**As of Phase 5 of the channels & identity unification refactor**, App MCP's half of this feature reads `Agent.example_prompts` — the same agent-level field used everywhere else on the platform (bundle revisions, A2A skills, MCP slash commands) — rather than a field on a now-deleted `AppAgentRoute`. Identity's half is unchanged: `IdentityAgentBinding.prompt_examples`, a dedicated newline-separated text field with its own validation.

## Core Concepts

| Term | Definition |
|------|-----------|
| **`Agent.example_prompts`** | A `list[str]` field on the agent itself — the App MCP side's example source. Edited from the agent's **Configuration** tab (Example Prompts modal), snapshotted onto bundle revisions at publish time, and already the sole source for MCP slash commands and A2A skills — this feature reads it, it does not own it |
| **`IdentityAgentBinding.prompt_examples`** | Optional newline-separated text field on an identity binding — the identity side's example source, prefixed with the owner's name before display |
| **MCP Prompt** | An entry in the MCP `prompts/list` response that MCP clients (Claude Desktop, Cursor) display as a suggested action |
| **Prefixing** | For identity bindings, each example is automatically wrapped with "ask {Owner Name} ({email}) to {example}" so callers address the right person |

## How It Works

### App MCP — from `Agent.example_prompts`

Owners write short task suggestions as a list on the agent's Configuration tab. Each non-empty entry becomes an individual MCP prompt returned via `prompts/list`, alongside the trigger-prompt-based prompt every eligible agent already gets.

Examples are returned **as-is** — they already skip the routing prefix, so MCP clients can send them directly to the `send_message` tool.

```
Agent.example_prompts:
  "generate employee report"
  "summarize last quarter sales"

MCP client prompts/list sees:
  "generate employee report"
  "summarize last quarter sales"
```

Because `example_prompts` is a general agent field, not something App MCP created, there is **no App MCP-specific length or count limit** on it (unlike `IdentityAgentBinding.prompt_examples` — see below). `ChannelCandidateProvider` joins the list into the newline-separated `Candidate.prompt_examples` string internally; that join happens at exactly one call site, deliberately not exposed as a reusable helper, so a third representation of "prompt examples" never sprouts.

### Identity — from `IdentityAgentBinding.prompt_examples`

Identity binding owners write the same short task descriptions, but as a dedicated newline-separated text field on the binding, unchanged by Phase 5. When building Stage 1's identity candidates, the system aggregates examples from all active bindings accessible to that caller and **prefixes each line** with the identity owner's name and email — necessary because identity messages go through two-stage routing: the caller must address the person first, then Stage 2 selects the agent.

```
Binding prompt_examples (owner = John Doe, john@example.com):
  "generate employee report"
  "summarize last quarter sales"

Caller's MCP client prompts/list sees:
  "ask John Doe (john@example.com) to generate employee report"
  "ask John Doe (john@example.com) to summarize last quarter sales"
```

If a caller has access to multiple bindings from the same owner, all examples are aggregated into a single combined list under that owner's identity candidate.

### Validation Rules

**Identity bindings only** — `IdentityAgentBinding.prompt_examples`:
- Maximum **2000 characters** total per field
- Maximum **10 non-empty lines** per binding
- Empty lines and whitespace-only lines are ignored (not counted)
- Validation runs on both create and update (POST and PUT)
- Violations return HTTP 422

**`Agent.example_prompts`** carries no dedicated length/count validation from this feature — it is a general-purpose agent field maintained by the agent-prompts feature, not by App MCP.

### Visibility Rules

- An agent with no `example_prompts` behaves exactly as before (only the trigger-prompt-based prompt is emitted).
- For identity bindings, only examples from bindings where the caller has an active, enabled assignment are included.
- Both fields are optional.

## User Flows

### Setting Prompt Examples on an Agent (App MCP)

1. Agent owner opens the agent's **Configuration** tab.
2. Opens the **Example Prompts** modal and adds entries, one per suggestion.
3. Saves — examples are stored on `Agent.example_prompts`.
4. Connected MCP clients see the examples in their prompt suggestions the next time they list prompts, alongside the trigger-prompt entry, as long as the agent is a routing candidate at all (a router trigger prompt or at least one example) — see [App MCP Server](app_mcp_server.md).

There is no App MCP-specific creation dialog any more — the agent's Integrations tab MCP Connectors "New" dialog no longer offers an "App MCP Server Integration" option.

### Setting Prompt Examples on an Identity Binding

1. Identity owner opens **Settings → Channels → Identity Server** card.
2. Adds or edits an agent binding via the "Add Agent" affordance (or the edit dialog on an existing binding).
3. Fills in the "Prompt Examples" textarea.
4. Saves — examples are stored on the binding.
5. Callers with active assignments see the prefixed examples in their MCP client.

This entry point moved here from the agent's Integrations tab ("Identity MCP Server Integration" is gone from that dialog) as part of the same phase that removed App MCP's route-based creation flow — see [Identity Routing](../identity_routing/identity_routing.md).

## Integration Points

- **[App MCP Server](app_mcp_server.md)** — `Agent.example_prompts` is read by `ChannelCandidateProvider`, the same provider [Server Channels](../server_channels/server_channels.md) uses; emitted in `prompts/list` via `app_prompts.py`
- **[Identity Routing](../identity_routing/identity_routing.md)** — `IdentityAgentBinding.prompt_examples` is aggregated and prefixed by `IdentityCandidateProvider` when building Stage 1 candidates for a caller
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — examples appear as MCP prompts in the standard `prompts/list` protocol response
- **[Auto Routing Tuning](../routing_tuning/routing_tuning.md)** — since that feature's Phase 5, `prompt_examples` is no longer purely an MCP-prompt-suggestion aid: the shared `AgentClassifier` renders each candidate's examples into the routing prompt sent to the LLM, so this field now directly influences which agent an ambiguous message is routed to. The routing-tuning card's near-miss ranking scores `prompt_examples` alongside `trigger_prompt` for the same reason
