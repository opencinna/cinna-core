# Prompt Examples -- Technical Details

**As of Phase 5 of the channels & identity unification refactor**, the App MCP side of this feature reads `Agent.example_prompts` (a pre-existing agent-level field), not a field on the now-deleted `AppAgentRoute`. Identity's side (`IdentityAgentBinding.prompt_examples`) is unchanged.

## File Locations

### Backend -- Models

- `backend/app/models/agents/agent.py` -- `Agent.example_prompts` (`list[str]`, `sa_column=Column(JSON)`, `default_factory=list`) — the App MCP side's example source, not a field this feature owns or validates
- `backend/app/models/identity/identity_models.py` -- `IdentityAgentBinding.prompt_examples` (Text, nullable); included in `IdentityAgentBindingCreate`, `IdentityAgentBindingUpdate`, `IdentityAgentBindingPublic` schemas

### Backend -- Routes (Validation)

- `backend/app/api/routes/identity.py` -- `_validate_prompt_examples()` for identity bindings; enforces 2000 char / 10 line limits with HTTP 422
- `Agent.example_prompts` has no dedicated validation route in this feature — it is written through the general agent update endpoints, maintained by the agent-prompts feature

### Backend -- Services

- `backend/app/services/routing/channel_candidate_provider.py` -- reads `agent.example_prompts` directly and joins the list into the newline-separated `Candidate.prompt_examples` string via `example_prompt_text()`, the one call site that performs this join
- `backend/app/services/routing/identity_candidate_provider.py` -- `IdentityCandidateProvider.build()` aggregates and prefixes `IdentityAgentBinding.prompt_examples` across a caller's accessible bindings, re-voiced as `"ask {name} ({email}) to {line}"`
- `backend/app/services/identity/identity_service.py` -- passes `prompt_examples` through on create and update; returns in `IdentityAgentBindingPublic`

### Backend -- MCP Prompts

- `backend/app/mcp/app_prompts.py` -- `register_app_mcp_prompts()` composes `ChannelCandidateProvider` + (gated) `IdentityCandidateProvider`, then iterates each candidate's `prompt_examples` string, splitting by newline and emitting each non-empty line as an individual `PromptMessage`. Candidate-shape-agnostic: it does not know or care whether a candidate's examples came from `Agent.example_prompts` or a binding's field

### Backend -- Migration

- `backend/app/alembic/versions/b5a73df91425_add_prompt_examples_to_routes_and_bindings.py` -- historical: added `prompt_examples` to `app_agent_route` (now dropped, migration `867cacb5a827`) and `identity_agent_binding` (survives)
- `Agent.example_prompts` predates this feature entirely and was added in a separate migration (agent-prompts feature)

### Frontend

- `frontend/src/components/Agents/EditExamplePromptsModal.tsx` -- edits `Agent.example_prompts` from the Configuration tab; the App MCP side's editing surface
- `frontend/src/components/UserSettings/IdentityServerCard.tsx` -- `editPromptExamples` state; "Prompt Examples" textarea in the add/edit binding dialog (now the sole creation entry point for identity bindings — the Integrations-tab dialog option is gone) with helper text explaining name prefixing

### Tests

- `backend/tests/api/app_mcp/prompt_examples_test.py` -- `IdentityAgentBinding` lifecycle and validation (>2000 chars, >10 lines, boundary). The `Agent.example_prompts` side of App MCP prompt listing is covered by the App MCP session/prompt tests reading the field, not by dedicated validation tests here, since this feature does not validate that field

## Database Schema

### `agent.example_prompts`

- Type: JSON (`list[str]`), `NOT NULL DEFAULT '[]'`
- No length or count constraint owned by this feature
- Predates this feature; also used by A2A skills, MCP slash commands, and bundle revision snapshots

### `identity_agent_binding.prompt_examples`

- Type: Text, nullable, default null
- Contains newline-separated short prompt strings
- No database-level length or line-count constraint (enforced at the API layer)

## Key Implementation Details

### App MCP — reading `Agent.example_prompts`

`ChannelCandidateProvider` reads `agent.example_prompts` (a `list[str]`) directly off the `Agent` row it is already iterating for eligibility (non-blank `router_trigger_prompt` OR non-empty `example_prompts`). The list is joined with newlines into the `Candidate.prompt_examples` string at exactly one call site — there is deliberately no shared "list → string" helper elsewhere, because a second representation of prompt examples is the trap this design avoids, not the join itself.

### Identity Example Aggregation

`IdentityCandidateProvider.build()` runs a join across `IdentityAgentBinding` and `IdentityBindingAssignment`, filtering for active bindings and active+enabled assignments for the target caller. Each non-empty line from matching bindings is prefixed with `"ask {owner_name} ({owner_email}) to {line}"` and all results are joined with newlines into the identity `Candidate.prompt_examples`.

### MCP Prompt Emission

In `app_prompts.py`, `list_available_agents()` iterates the composed candidate list (owned agents, then identities when enabled). For each candidate:
1. Always emits `trigger_prompt` as a `PromptMessage`.
2. If `prompt_examples` is set (non-empty string, regardless of source), splits by newline and emits each non-empty stripped line as an additional `PromptMessage`.

### Validation

Only `identity.py`'s `_validate_prompt_examples()` survives (the equivalent function in the now-deleted `agent_app_mcp_routes.py` is gone with the route). It:
- Returns early if value is `None` or empty.
- Checks total length > 2000 → HTTP 422.
- Counts non-empty lines > 10 → HTTP 422.
- Is called before service-layer create/update.
