# Agent REST API — Spec Viewer

## Purpose

Render a producer agent's harvested OpenAPI spec as friendly, read-only API docs in a dedicated browser tab — instead of dumping raw JSON into a dialog. Both the producer ("Agent REST API" card) and the consumer (the `agent_api` connection credential) reach the same viewer through their **View Spec** button.

## Core Concepts

- **Spec Viewer** — a standalone full-page route that fetches the harvested spec and renders it as docs (grouped endpoints, method badges, parameters, request/response schemas).
- **New-tab launch** — View Spec opens the viewer in its own browser tab (a stable per-agent window name, so re-clicking focuses the existing tab rather than stacking duplicates).
- **Authenticated by the app shell** — the viewer is a normal in-app route, so the tab reuses the same JWT (from `localStorage`) the rest of the app uses; the spec is fetched through the authenticated client, with no separate token handling.
- **Read-only** — the viewer renders docs only. It deliberately has **no request playground**: a live call would have to go through the authenticated backend proxy (not the raw server URL embedded in the spec), so trying endpoints lives with the proxy/credential flow, not here.

## User Flows

### Producer owner previews their API
1. On the agent's Integrations tab, the "Agent REST API" card shows a **View Spec** button when the API is enabled.
2. Clicking it opens the Spec Viewer in a new tab for that agent.
3. The viewer fetches the harvested spec (owner endpoint) and renders the endpoints, parameters, and schemas the producer's code currently exposes.

### Consumer inspects a connection's surface
1. On an `agent_api` connection credential's detail page, **View Spec** is enabled when the producer agent is still resolvable.
2. Clicking it opens the Spec Viewer for the **producer** agent — showing exactly the endpoints that connection can call.

## Business Rules

- **Source of truth is the same harvested spec.** The viewer reads the cached, import-only-harvested OpenAPI document (see [OpenAPI Spec is Always Accurate](agent_api.md)) — it does not start the serving child and does not introduce a new spec source.
- **Auth is inherited, not re-implemented.** Because the viewer is an in-app route, an unauthenticated tab is bounced to login; an authenticated tab fetches the spec exactly as any other request would.
- **Read-only / no live calls.** No "Try it"/"Send request" affordance, no client/SDK export, no third-party branding — only rendered docs.
- **Graceful failure.** If the spec cannot be loaded (producer API stopped or failing to build), the viewer shows a short "could not load" message pointing the user back to the agent's build status, mirroring the producer card's error surfacing.
- **Consumer access depends on producer resolvability.** The consumer's View Spec button is disabled when the producer agent is no longer accessible.

## Architecture Overview

```
View Spec button (producer card / connection credential)
   └─ opens new tab → Spec Viewer route (/agent-api-spec/:agentId)
        └─ authenticated client → GET /agents/{id}/agent-api/openapi.json (cached spec)
             └─ OpenApiSpecViewer renders grouped endpoints + schemas (read-only)
```

## Integration Points

- **[Agent REST API](agent_api.md)** — the Spec Viewer is the rendering surface for the harvested spec; it changes how View Spec behaves (rendered docs in a new tab) without changing the spec source, the harvest pipeline, or the proxy.
- **[Agent Credentials](../agent_credentials/agent_credentials.md)** — the consumer entry point is the `agent_api` connection credential detail page; the viewer shows the producer's surface that the credential proxies.
- **Implementation details:** see [spec_viewer_tech.md](spec_viewer_tech.md).

---

*Last updated: 2026-05-26*
