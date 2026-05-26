# Agent REST API — Spec Viewer (Tech)

Implementation of the read-only OpenAPI docs viewer reached from the **View Spec** buttons. Built entirely from the app's own primitives — no external API-docs library.

## File Locations

### Frontend

- `frontend/src/routes/agent-api-spec/$agentId.tsx` — standalone full-page route (`/agent-api-spec/:agentId`). `beforeLoad` redirects to `/login` when `isLoggedIn()` is false; the component fetches the spec via `useAgentApiSpec(agentId, true)` and renders `OpenApiSpecViewer`, with loading (`PendingItems`) and "could not load" fallback states.
- `frontend/src/components/Agents/OpenApiSpecViewer.tsx` — the renderer. Takes the spec object and renders grouped operations, method badges, parameter tables, and request/response schemas with `$ref` resolution.
- `frontend/src/utils/agentApiSpec.ts` — `openAgentApiSpec(agentId)`; opens the route in a stable-named tab (`cinna-agent-api-spec-<agentId>`) so re-clicks reuse it.
- `frontend/src/components/Agents/AgentRestApiCard.tsx` — producer card; **View Spec** button calls `openAgentApiSpec(agentId)` (no more in-card dialog/spec fetch).
- `frontend/src/components/Credentials/AgentApiConnectionView.tsx` — consumer connection panel; **View Spec** calls `openAgentApiSpec(producerAgentId)`, disabled when the producer is unresolved.
- `frontend/src/hooks/useAgentApi.ts` — `useAgentApiSpec(agentId, enabled)` (query key `["agentApiSpec", agentId]`); now consumed by the route rather than the two cards.

## Data Flow

- The route is a normal SPA route, so the new tab loads the app shell (`main.tsx`) which sets `OpenAPI.TOKEN` from `localStorage["access_token"]`. The spec request is therefore authenticated exactly like every other client call — no separate auth path.
- `useAgentApiSpec` calls `AgentApiService.getAgentApiSpec({ agentId })` → `GET /api/v1/agents/{agent_id}/agent-api/openapi.json` (owner endpoint; served from the env cache). See `agent_api_tech.md` for the backend route and harvest pipeline.

## OpenApiSpecViewer Internals

- **Operation grouping** — flattens `paths` × HTTP methods (`get/post/put/patch/delete/head/options`) into operations, grouped by the operation's first `tag` (fallback group `"Endpoints"`); per-endpoint expand/collapse plus an "Expand all / Collapse all" toggle.
- **Method badges** — colour-coded via `METHOD_STYLES` (get=emerald, post=sky, put=amber, patch=violet, delete=rose, head/options=gray).
- **`$ref` resolution** — `resolveRef()` walks local pointers (`#/components/schemas/X`, `#/$defs/X`, JSON-pointer `~0`/`~1` unescaped); `deref()` follows one level keeping the ref name as a type label; `refName()` is the last path segment.
- **Schema rendering** — `SchemaView` recurses objects (property rows), arrays (item schema), and combinators (`allOf` merged; `anyOf`/`oneOf` listed). Cycle-guarded by a `seen` set of ref names and a hard `depth > 8` cap. `PropertyRow` lazily expands nested object/array-of-object properties. `typeLabel()` renders FastAPI-style `anyOf: [X, {type: null}]` as `X | null`.
- **Parameters / responses** — `ParameterSection` groups by `in` (path/query/header/cookie); `ResponsesSection` colours status codes by class (2xx/3xx/4xx/5xx); `BodySchema` prefers a JSON media type.
- **Constraints & enums** — `constraintChips()` surfaces `default`/min/max/length/items/`pattern`/`format`; `EnumChips` renders enum values.
- **Markdown** — descriptions (info, operation, parameter, schema) render via `react-markdown` + `remark-gfm` with `prose` classes (Tailwind typography).
- **Theming** — uses shadcn tokens (`bg-background`, `text-muted-foreground`, `Badge`); inherits the app's light/dark theme.

## Routing

- File-based (TanStack Router). The route lives **outside** `_layout/` (no app chrome/sidebar), like `routes/dashboard-fullscreen/$dashboardId.tsx`. `routeTree.gen.ts` is regenerated automatically by the router vite plugin.

## Notes

- **Read-only by design.** No request playground: a live call must traverse the authenticated proxy (`/agents/{id}/agent-api/proxy/{path}` or the consumer proxy), not the spec's server URL, so it is intentionally omitted here.
- The viewer treats the spec as a dynamically-shaped document (loose typing); it does not depend on the generated client types for spec contents.

---

*Last updated: 2026-05-26*
