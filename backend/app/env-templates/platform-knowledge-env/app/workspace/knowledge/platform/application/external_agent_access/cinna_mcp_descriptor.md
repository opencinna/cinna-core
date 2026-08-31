# `cinna.mcp` Descriptor

## Purpose

The `cinna.mcp` descriptor lets the Cinna Desktop app wrap any reachable agent as an **emulated MCP tool** — presenting it to a local LLM orchestrator the same way a native MCP server would appear. Without this descriptor the desktop would have to reverse-engineer each agent's identity from its raw A2A card; with it, the desktop has everything needed to construct a well-typed tool entry (name, description, input schema, capabilities, example prompts, run commands) in a single, version-stable JSON object.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **`cinna.mcp` descriptor** | A JSON object describing how to present a single agent as an MCP tool. Versioned (`version: 1`). Consumed verbatim by Cinna Desktop |
| **`urn:cinna:mcp` extension** | The A2A `AgentExtension` entry (URI `urn:cinna:mcp`) on the extended agent card that carries the descriptor in its `params` field |
| **Discovery `mcp` field** | A mirror of the descriptor attached directly to each `ExternalTargetPublic` in the `GET /api/v1/external/agents` response — the refresh-friendly cache path |
| **Tool name slug** | Stable, lowercase, `[a-z0-9_]+` identifier derived from the agent's name. Collision-free across a user's full reachable set via deterministic suffixing |
| **Stateful vs. stateless input schema** | MCP connector variant keeps `context_id` for round-tripping; desktop variant omits it — the desktop maintains session continuity itself and injects it at the A2A level |
| **Shared contract** | `backend/app/mcp/tool_contracts.py` defines the canonical `send_message` tool name, description, and schema in one place. Both the MCP connector and the desktop descriptor import from it, so the two surfaces cannot drift |

---

## Two Delivery Channels

The descriptor is emitted through two independent paths so the desktop can obtain it efficiently regardless of which call it makes:

### 1. Extended A2A Agent Card (`capabilities.extensions[]`)

When a client fetches the extended agent card from either external A2A endpoint
(`GET /api/v1/external/a2a/agent/{id}/` or `/identity/{id}/` — the `/route/{id}/`
endpoint is deleted, along with the target kind it served), the
`capabilities.extensions` array includes an entry with `uri = "urn:cinna:mcp"`.
The descriptor lives in that entry's `params` field.

The card is the authoritative source and carries the full descriptor including the `run_commands[]` array. It survives both A2A protocol versions (v0.3 and v1.0) because the v1.0 adapter preserves `capabilities.extensions`.

Identity cards (person-level routing) carry no `urn:cinna:mcp` extension because an identity resolves to multiple agents dynamically — there is no single tool to wrap.

### 2. Discovery Payload (`GET /api/v1/external/agents`)

The `ExternalTargetPublic` schema has a top-level `mcp` field that mirrors the descriptor. The desktop re-fetches this endpoint on its background sync (roughly every five minutes), so it can update descriptors for all agents in one request without re-fetching every individual card.

- For `target_type="agent"` targets the `mcp` field is populated.
- For `target_type="identity"` the `mcp` field is `null` — same reason as above. There is no third target type any more (`app_mcp_route` was removed in Phase 5 of the channels & identity unification, along with the `AppAgentRoute` family).

The desktop treats the card as source of truth and the discovery payload as the refreshable cache. Both carry the same descriptor shape; the desktop must accept either.

---

## Descriptor Shape

```jsonc
{
  "version": 1,
  "tool_name": "email_agent",
  "display_name": "Email Agent",
  "description": "Send a self-contained task or question to the AI agent and receive a response. The agent can use tools, write code, and perform tasks based on your message.\n\nSend anything related to sending or organizing email.",
  "input_schema": {
    "type": "object",
    "properties": {
      "message": { "type": "string", "description": "The task or question for the agent." }
    },
    "required": ["message"]
  },
  "capabilities": { "files": true, "resources": false, "run_commands": true },
  "example_prompts": ["send the weekly digest", "summarize my inbox"],
  "run_commands": [
    { "name": "send_digest", "description": "Send the weekly email digest", "invocation": "/run:send_digest" }
  ]
}
```

### Field-by-field notes

| Field | Source | Notes |
|-------|--------|-------|
| `version` | Constant (`CINNA_MCP_DESCRIPTOR_VERSION = 1`) | Bump on breaking shape change |
| `tool_name` | Slugified `Agent.name`, deconflicted | `[a-z0-9_]+`, capped at 48 chars |
| `display_name` | `Agent.name` | Human-readable label in the desktop tool list |
| `description` | Base + `router_trigger_prompt` folded in | Base + `Agent.router_trigger_prompt` when set; base + `Agent.description` when only that is set; base only when neither is set |
| `input_schema` | Canonical schema from `tool_contracts.py` | `message` only — no `context_id` |
| `capabilities.files` | Always `true` | `get_file_upload_url` is unconditionally registered for every MCP connector |
| `capabilities.resources` | Always `false` (v1) | Workspace resources are listed dynamically; out of scope |
| `capabilities.run_commands` | `bool(cli_commands_parsed)` | Mirrors the `cinna.run.*` A2A skill source |
| `example_prompts` | `Agent.example_prompts` | Empty list when unset |
| `run_commands` | `AgentEnvironment.cli_commands_parsed` | Each entry: `{name, invocation, description?}`. `invocation` = `/run:{name}`. `description` omitted when absent |

---

## Tool Name Slug — Determinism and Collision Handling

There is no `slug` field on `Agent`. The slug is generated at response time from the agent's name:

1. Lowercase the source name.
2. Replace any run of non-`[a-z0-9]` characters with a single underscore.
3. Strip leading and trailing underscores.
4. Cap at 48 characters (strip trailing underscore after truncation).
5. Fall back to `"agent"` if the result is empty.

Collision detection runs across the user's **full reachable set** (both sections of the discovery response — personal agents, since identity targets carry no descriptor to collide) in a single pass. When two or more targets produce the same base slug, every colliding entry is suffixed with the first 8 hex characters of its `agent.id` (no hyphens) — making the result deterministic regardless of ordering or re-request timing.

Example: two agents both named "Reports" → `reports_3f2a1b4c` and `reports_9d8e7f0a`.

The desktop performs its own sanitization as a safety net and to handle agents from other platforms that emit no descriptor at all. Backend determinism is the goal, not a hard contract.

---

## Per-Target-Type Behavior

| Target type | `display_name` source | `description` source | `tool_name` slug source |
|-------------|----------------------|---------------------|------------------------|
| `agent` (personal) | `Agent.name` | `SEND_MESSAGE_DESKTOP_DESCRIPTION` + `router_trigger_prompt` (or `Agent.description`) | `Agent.name` |
| `identity` | N/A | N/A | N/A — `mcp = null` |

There were previously three rows here: a route target (`app_mcp_route`) built its descriptor from `AppAgentRoute.name` and `trigger_prompt` rather than the raw underlying agent, so the tool name and description matched what the caller saw in the A2A card rather than the agent's own identity. That target type — and the entire route mechanism behind it — was deleted in Phase 5 of the channels & identity unification; every remaining target's descriptor reads straight off the agent (or, for identity, is absent).

---

## Graceful Degradation

Cinna Desktop degrades gracefully when the descriptor is absent. It synthesizes a fallback descriptor from `Agent.name`, `Agent.description`, and `Agent.example_prompts` on the raw A2A card. This means:
- The backend can ship the descriptor independently of the desktop.
- The desktop automatically starts using the richer descriptor once it appears.
- Older platform instances (no `mcp` field on the discovery payload) are still usable.

---

## Business Rules

- The descriptor rides on the **extended (authenticated) agent card** built by `build_agent_card`, so it appears on the authenticated extended card of **both** the internal (`/api/v1/a2a/{id}/`) and external (`/api/v1/external/a2a/...`) A2A surfaces. It is **not** present on the minimal public card returned to unauthenticated callers. The discovery `mcp` mirror (`/api/v1/external/agents`) is external-surface only. Cinna Desktop consumes the descriptor exclusively from the external surface.
- Slug deconfliction is computed per-user per-request. The desktop sanitizes again client-side as a final safety net.
- `run_commands` entries omit `description` when none is provided in `CLI_COMMANDS.yaml` rather than serializing `"description": null` inside the descriptor dict.
- `input_schema` intentionally omits `context_id` on the desktop variant. The MCP connector's `send_message` keeps it; the desktop's emulated tool does not.

---

## Integration Points

- **[External Agent Access](./external_agent_access.md)** — the discovery endpoint (`GET /api/v1/external/agents`) and external A2A card endpoints that deliver the descriptor
- **[A2A Protocol](../a2a_integration/a2a_protocol/a2a_protocol.md)** — the `AgentCard.capabilities.extensions[]` slot where the `urn:cinna:mcp` entry lives; the v1.0 adapter preserves it
- **[MCP Integration](../mcp_integration/agent_mcp_architecture.md)** — the per-agent MCP connector also exposes `send_message`; the canonical tool contract is shared to prevent drift
- **[CLI Commands](../../agents/cli_commands/cli_commands.md)** — `AgentEnvironment.cli_commands_parsed` is the source for both `cinna.run.*` A2A skills and the descriptor's `run_commands[]` array
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — the desktop JWT issued by the desktop auth flow is the credential used to fetch both the A2A card and the discovery payload

---

*Last updated: 2026-08-25 — Phase 5 of the channels & identity unification refactor removed the `app_mcp_route` target type and its route-identity overrides*
