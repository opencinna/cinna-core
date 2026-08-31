# `cinna.mcp` Descriptor — Technical Details

## File Locations

### Backend — Contract / Single Source of Truth
- `backend/app/mcp/tool_contracts.py` — **new** canonical `send_message` constants and `build_send_message_input_schema(include_context_id=...)`. Imported by both `app/mcp/tools.py` (MCP connector) and `app/services/a2a/a2a_service.py` (descriptor builder)

### Backend — A2A Service
- `backend/app/services/a2a/a2a_service.py` — descriptor builder and A2A card attachment:
  - `CINNA_MCP_EXTENSION_URI = "urn:cinna:mcp"`
  - `CINNA_MCP_DESCRIPTOR_VERSION = 1`
  - `A2AService.slugify_tool_name(name)` — converts any string to a `[a-z0-9_]+` slug ≤ 48 chars
  - `A2AService.deconflict_tool_name(base_slug, discriminator)` — appends the first 8 hex chars of `discriminator` (a UUID) to resolve slug collisions deterministically
  - `A2AService._build_run_command_descriptors(environment)` — builds `run_commands[]` from `cli_commands_parsed`
  - `A2AService.build_cinna_mcp_descriptor(agent, environment, *, tool_name, display_name=None, description=None)` — builds the full descriptor dict
  - `A2AService.build_agent_card(...)` — `urn:cinna:mcp` extension appended via `AgentCapabilities(extensions=[...])`; accepts `mcp_tool_name`, `mcp_display_name`, `mcp_description` kwargs

### Backend — External A2A Service (card builder)
- `backend/app/services/external/external_a2a_service.py` — `ExternalA2AService._build_agent_card()` calls `A2AService.get_agent_card_dict()` for `target_type="agent"`. Identity cards (`_build_identity_card`) emit no `urn:cinna:mcp` extension (person-level, not single-tool). **Deleted in Phase 5 of the channels & identity unification:** `_build_route_card(...)` and the `mcp_tool_name`/`mcp_display_name`/`mcp_description` override plumbing it used — there is no route target left to reflect

### Backend — Discovery Catalog Service
- `backend/app/services/external/external_agent_catalog_service.py`:
  - `_DescriptorContext` dataclass — holds `(agent, environment)` collected during the personal-agents section pass
  - `ExternalAgentCatalogService._attach_mcp_descriptors(targets, contexts)` — two-pass deconfliction; first pass counts base-slug occurrences (from `agent.name`); second pass suffixes collisions and calls `build_cinna_mcp_descriptor`
  - `ExternalAgentCatalogService._list_personal_agents(...)` — records a `_DescriptorContext` per agent (slug source = `agent.name`)
  - `ExternalAgentCatalogService._list_identity_contacts(...)` — no `_DescriptorContext` recorded; `mcp` field stays `null`

**Deleted in Phase 5 of the channels & identity unification:** `_list_mcp_shared_agents(...)` (the middle discovery section, built from `AppAgentRouteService.get_effective_routes_for_user`) and the `AppAgentRouteService`/`EffectiveRoute` module it read from — there is no route-shaped descriptor context left to build.

### Backend — Models
- `backend/app/models/external/external_agents.py` — `ExternalTargetPublic.mcp: dict[str, Any] | None = None` — plain `pydantic.BaseModel` field (not SQLModel), safe from the `metadata` shadow issue

---

## `tool_contracts.py` API

```python
SEND_MESSAGE_TOOL_NAME: str          # "send_message"
SEND_MESSAGE_DESCRIPTION: str        # Stateless MCP connector description (keeps context_id)
SEND_MESSAGE_DESKTOP_DESCRIPTION: str # Stateful desktop description (no context_id mention)
SEND_MESSAGE_ARG_DESCRIPTION: str    # "The task or question for the agent."

def build_send_message_input_schema(*, include_context_id: bool) -> dict[str, Any]:
    # include_context_id=True  → {message, context_id}  (MCP connector)
    # include_context_id=False → {message}               (desktop descriptor)
```

---

## `A2AService` Descriptor API

### `slugify_tool_name(name: str | None) -> str`

```
"Email Agent"  → "email_agent"
"Q3 Sales!"    → "q3_sales"
""             → "agent"
"a" * 60       → "aaa...a" (truncated to 48 chars)
```

### `deconflict_tool_name(base_slug: str, discriminator: UUID | str) -> str`

Appends the first 8 hex chars of `discriminator` (hyphens stripped) with an underscore separator. Combined length is capped at 48 chars by trimming `base_slug` before appending.

```
deconflict_tool_name("reports", UUID("3f2a1b4c-...")) → "reports_3f2a1b4c"
```

### `build_cinna_mcp_descriptor(agent, environment, *, tool_name, display_name=None, description=None) -> dict`

| Param | Type | Effect |
|-------|------|--------|
| `agent` | `Agent` | Source of `name`, `router_trigger_prompt`, `description`, `example_prompts` |
| `environment` | `AgentEnvironment \| None` | Source of `cli_commands_parsed` for `run_commands` and `capabilities.run_commands` |
| `tool_name` | `str` | Pre-computed, deconflicted slug. Caller is responsible for deconfliction |
| `display_name` | `str \| None` | Overrides `agent.name`. Unused by either remaining call site as of Phase 5 (the route builder that used it is deleted) — kept as a general override, not dead code |
| `description` | `str \| None` | Full description override. When `None`, `SEND_MESSAGE_DESKTOP_DESCRIPTION` + `router_trigger_prompt` (or `agent.description`) is constructed. When set, used verbatim |

### `build_agent_card(...)` kwargs for descriptor override

```python
A2AService.build_agent_card(
    agent, environment, base_url,
    mcp_tool_name="...",     # pre-computed slug; falls back to slugify(agent.name)
    mcp_display_name="...",  # display name override
    mcp_description="...",   # description override
)
```

The `urn:cinna:mcp` extension is always appended regardless of `a2a_config.enabled`. It is attached as:

```python
AgentExtension(
    uri="urn:cinna:mcp",
    description="Descriptor for wrapping this agent as an emulated MCP tool (Cinna Desktop).",
    params=A2AService.build_cinna_mcp_descriptor(...),
    required=False,
)
```

---

## Descriptor Flow: Discovery Endpoint

```
GET /api/v1/external/agents
        │
        ▼
ExternalAgentCatalogService.list_targets()
        │
        ├── _list_personal_agents()    → ExternalTargetPublic list
        │       └── records _DescriptorContext(agent, environment, slug_source=agent.name)
        │
        ├── _list_identity_contacts()  → ExternalTargetPublic list
        │       └── no _DescriptorContext (mcp stays null)
        │
        └── _attach_mcp_descriptors(targets, descriptor_contexts)
                ├── Pass 1: slugify each slug_source; count occurrences
                └── Pass 2: deconflict collisions; set target.mcp via build_cinna_mcp_descriptor
```

---

## Descriptor Flow: A2A Card Endpoint

```
GET /api/v1/external/a2a/{type}/{id}/
        │
        ▼
ExternalA2AService.build_card()
        │
        ├── target_type="agent"
        │       └── A2AService.get_agent_card_dict(agent, environment, ...)
        │               └── build_agent_card() appends urn:cinna:mcp extension
        │                       (slug from slugify(agent.name) — single-card path)
        │
        └── target_type="identity"
                └── synthesized AgentCard — no urn:cinna:mcp extension
```

Note: The card endpoint does not run cross-set deconfliction. If a user has two agents that produce the same base slug, the card path returns non-deconflicted slugs. The discovery path is the authoritative source for deconflicted slugs; the desktop should use that.

---

## Protocol Version Compatibility

The `urn:cinna:mcp` extension survives both A2A protocol versions:

| Protocol | Path | Extension preserved? |
|----------|------|---------------------|
| v0.3 | `?protocol=v0.3` or `/api/v1/a2a/v0.3/{id}/` | Yes — library-native card, no transform |
| v1.0 | Default or `/api/v1/a2a/v1.0/{id}/` | Yes — `A2AV1Adapter.transform_agent_card_outbound` preserves `capabilities.extensions` |

The v1.0 adapter at `backend/app/services/a2a/a2a_v1_adapter.py` rebuilds the outbound card from a key allowlist but explicitly carries over `capabilities.extensions` (see the adapter's `transform_agent_card_outbound` method).

---

## `ExternalTargetPublic` Schema Change

File: `backend/app/models/external/external_agents.py`

```python
class ExternalTargetPublic(BaseModel):
    ...
    mcp: dict[str, Any] | None = None
```

This is the only schema change touching the discovery response. No database migration is required — `ExternalTargetPublic` is a response-only Pydantic model (no DB table, `table=True` not set).

---

## `EffectiveRoute.name` — Removed

`EffectiveRoute` (and the `AppAgentRouteService` module it lived on) no longer exist as of Phase 5 of the channels & identity unification. There is no route-name field to carry any more — every remaining discovery target's descriptor reads straight off the underlying `Agent`.

---

## Tests

- `backend/tests/api/external/external_agents_test.py` — discovery endpoint tests; `mcp` field assertions for both target types (personal agent, identity), plus slug uniqueness and determinism across the response
- `backend/tests/api/external/external_a2a_agent_test.py` — agent card carries the `urn:cinna:mcp` extension with a well-formed descriptor across both v0.3 and v1.0
- `backend/tests/unit/test_cinna_mcp_descriptor.py` — pure-Python unit tests for `build_cinna_mcp_descriptor`, `slugify_tool_name`, `deconflict_tool_name`, and card extension attachment

**Deleted in Phase 5:** `backend/tests/api/external/external_a2a_route_test.py` — asserted the route card descriptor reflected the route identity (name + trigger prompt) rather than the underlying agent; there is no route card left to test <!-- nocheck -->

---

*Last updated: 2026-08-25 — Phase 5 of the channels & identity unification refactor removed the `app_mcp_route` target type, `_build_route_card`, `_list_mcp_shared_agents`, and `EffectiveRoute`*
