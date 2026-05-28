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
- `backend/app/services/external/external_a2a_service.py` — `ExternalA2AService._build_route_card(...)` passes `mcp_tool_name`, `mcp_display_name`, `mcp_description` to `A2AService.get_agent_card_dict()` so the descriptor reflects the route identity, not the raw underlying agent. Identity cards (`_build_identity_card`) emit no `urn:cinna:mcp` extension (person-level, not single-tool)

### Backend — Discovery Catalog Service
- `backend/app/services/external/external_agent_catalog_service.py`:
  - `_DescriptorContext` dataclass — holds `(agent, environment, slug_source, display_name, description)` collected during per-section passes
  - `ExternalAgentCatalogService._attach_mcp_descriptors(targets, contexts)` — two-pass deconfliction; first pass counts base-slug occurrences; second pass suffixes collisions and calls `build_cinna_mcp_descriptor`
  - `ExternalAgentCatalogService._list_personal_agents(...)` — records a `_DescriptorContext` per agent (slug source = `agent.name`)
  - `ExternalAgentCatalogService._list_mcp_shared_agents(...)` — records a `_DescriptorContext` per route (slug source = `route.name or route.agent_name`; `display_name` = route name; `description` = `route.trigger_prompt`)
  - `ExternalAgentCatalogService._list_identity_contacts(...)` — no `_DescriptorContext` recorded; `mcp` field stays `null`

### Backend — App MCP Route Service
- `backend/app/services/app_mcp/app_agent_route_service.py` — `EffectiveRoute` dataclass now includes `name: str = ""` (the route's own display name distinct from `agent_name`). Used by the catalog service as the slug source and display name for route descriptors

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
| `display_name` | `str \| None` | Overrides `agent.name`. Route builder passes the route name |
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
        ├── _list_mcp_shared_agents()  → ExternalTargetPublic list
        │       └── records _DescriptorContext(agent, environment,
        │               slug_source=route.name or agent_name,
        │               display_name=route.name, description=route.trigger_prompt)
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
        ├── target_type="app_mcp_route"
        │       └── A2AService.get_agent_card_dict(agent, environment, ...,
        │               mcp_tool_name=slugify(route.name),
        │               mcp_display_name=route.name,
        │               mcp_description=route.trigger_prompt)
        │               └── build_agent_card() appends urn:cinna:mcp extension
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

## `EffectiveRoute.name` Addition

File: `backend/app/services/app_mcp/app_agent_route_service.py`

`EffectiveRoute` dataclass gained a `name: str = ""` field. This field carries `AppAgentRoute.name` (the route's own display label). Before this change the catalog service had to fall back to `agent_name` for all route descriptors; now it can use the route's actual name when set, matching the identity the route card presents to the caller.

---

## Tests

- `backend/tests/api/external/external_agents_test.py` — discovery endpoint tests; `mcp` field assertions for all three target types (personal agent, shared route, identity), plus slug uniqueness and determinism across the response
- `backend/tests/api/external/external_a2a_agent_test.py` — agent card carries the `urn:cinna:mcp` extension with a well-formed descriptor across both v0.3 and v1.0
- `backend/tests/api/external/external_a2a_route_test.py` — route card descriptor reflects the route identity (name + trigger prompt), not the underlying agent
- `backend/tests/unit/test_cinna_mcp_descriptor.py` — pure-Python unit tests for `build_cinna_mcp_descriptor`, `slugify_tool_name`, `deconflict_tool_name`, and card extension attachment

---

*Last updated: 2026-05-28*
