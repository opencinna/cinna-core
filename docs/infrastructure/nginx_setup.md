# Nginx Setup

## Purpose

The Cinna platform relies on nginx in two places:

- **Production reverse proxy** — terminates TLS and routes requests to the backend (`/api/`, `/mcp/`, `/ws/`, select `/.well-known/*` URIs) or to the frontend SPA (everything else).
- **Frontend container nginx** (`frontend/nginx.conf`) — serves the static SPA build and proxies origin-root well-known URIs to the backend so specs-mandated discovery paths work when the frontend container is the entry point (local docker-compose).

This doc lists the required `location` blocks and links each one to the feature that depends on it.

## Required Location Blocks

### `/api/`

**Feature:** all backend API traffic.
**Upstream:** backend (`localhost:8000` in production, `backend:8000` inside docker-compose).
**Proxy headers:** `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`.

Present only in the production reverse proxy. In local dev the SPA calls the backend directly via `VITE_API_URL` (see `docker-compose.yml`), so `frontend/nginx.conf` does not proxy `/api/`.

### `/mcp/`

**Feature:** MCP protocol endpoints — see [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md) and [App MCP Server](../application/app_mcp_server/app_mcp_server_tech.md).
**Special requirements:** MCP Streamable HTTP uses SSE. `proxy_buffering off`, `proxy_cache off`, and `proxy_read_timeout 300s` are required so events flow in real time and long-lived streams don't get cut.
**Upstream:** backend.

### `/ws/`

**Feature:** Socket.IO real-time events — see [Realtime Events](../application/realtime_events/event_bus_system.md).
**Special requirements:** WebSocket upgrade (`proxy_http_version 1.1`, `Upgrade`/`Connection` headers).
**Upstream:** backend.

### `/api/v1/cli/`

**Feature:** Cinna CLI agent-scoped traffic — see [Cinna CLI Integration](../application/cinna_cli_integration/cinna_cli_integration.md).
**Why a dedicated block:** two endpoints under this prefix need transport that the plain `/api/` block doesn't provide:

- `WS /api/v1/cli/agents/{agent_id}/sync-stream` — long-lived Mutagen tunnel over WebSocket.
- `POST /api/v1/cli/agents/{agent_id}/exec` — Server-Sent Events stream.

**Special requirements:** WebSocket upgrade (`proxy_http_version 1.1`, `Upgrade`/`Connection` headers), `proxy_buffering off`, `proxy_cache off`, and a long `proxy_read_timeout`/`proxy_send_timeout` (3600s) so idle sync sessions and SSE streams aren't cut mid-flight. Must be declared before the generic `/api/` block (nginx matches longest-prefix, but ordering it first keeps intent clear).

**Upstream:** backend.

### `/.well-known/oauth-protected-resource`

**Feature:** RFC 9728 Protected Resource Metadata for MCP OAuth — see [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md) and [MCP Connector Setup](../application/mcp_integration/mcp_connector_setup.md).
**Why at origin root:** RFC 9728 requires the metadata document at the domain root, not under an API prefix.
**Upstream:** backend.

### `/.well-known/oauth-authorization-server`

**Feature:** RFC 8414 Authorization Server Metadata for MCP OAuth — see [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md).
**Why at origin root:** RFC 8414 requires the metadata document at the domain root.
**Upstream:** backend.

### `/.well-known/cinna-desktop`

**Feature:** Cinna Desktop instance discovery — see [Desktop App Authentication](../application/desktop_auth/desktop_auth.md).
**Why at origin root:** The Cinna Desktop app fetches `https://{instance}/.well-known/cinna-desktop` before the user logs in to verify the instance and learn its OAuth URLs. It has no knowledge of API prefixes yet.
**Upstream:** backend.

### `/agent-hooks/`

**Feature:** Agent Webhooks public execution endpoint — see [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md).
**Why not under `/api/`:** Mounted at the app root (no `/api/v1` prefix) to match the task-trigger `/hooks/` convention and produce short, shareable URLs (`{host}/agent-hooks/{webhook_id}`).
**Upstream:** backend.

Note: `/hooks/` (task triggers) carries the same requirement. Both paths must appear in the production reverse proxy config. In local docker-compose mode requests reach the backend directly via `VITE_API_URL`, so no `frontend/nginx.conf` block is needed.

### `/`

**Feature:** frontend SPA. `try_files $uri $uri/ /index.html` to support client-side routing.
**Upstream:** static files (served by nginx itself, no proxy).

## Configuration Files

- `frontend/nginx.conf` — in-container nginx config baked into the frontend Docker image. Currently serves the SPA and proxies the three `.well-known/*` URIs above. Does **not** proxy `/api/`, `/mcp/`, `/ws/` because the SPA talks to the backend directly via `VITE_API_URL` in local/docker-compose mode.
- `frontend/nginx-backend-not-found.conf` — mountable snippet that returns 404 for `/api`, `/docs`, `/redoc` when the frontend container is used without a backend in front.
- Production reverse-proxy config — lives in deployment infrastructure (outside this repo). Must include all location blocks listed above.

## Adding a New Well-Known URI

When a new feature introduces a `/.well-known/*` endpoint:

1. Register the route at the app level in `backend/app/main.py` (not under `/api/v1/`).
2. Add a matching `location /.well-known/{name}` proxy block to `frontend/nginx.conf`.
3. Add the same block to the production reverse proxy (deployment infra).
4. Add a subsection to this doc under **Required Location Blocks** referencing the feature doc.

## Integration Points

- [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md) — uses `/mcp/`, `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`
- [App MCP Server](../application/app_mcp_server/app_mcp_server.md) — shares the `/mcp/` routing with per-agent MCP servers
- [Desktop App Authentication](../application/desktop_auth/desktop_auth.md) — uses `/.well-known/cinna-desktop`
- [Realtime Events](../application/realtime_events/event_bus_system.md) — uses `/ws/`
- [Cinna CLI Integration](../application/cinna_cli_integration/cinna_cli_integration.md) — uses `/api/v1/cli/` (WebSocket sync tunnel + SSE exec stream)
- [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md) — uses `/agent-hooks/` (public webhook execution, no JWT)
- [Task Triggers](../application/input_tasks/task_triggers.md) — uses `/hooks/` (same pattern as agent webhooks, public webhook execution, no JWT)
