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

### `/api/v1/env-console/`

**Feature:** Agent Environment Console — live container-logs follow (`WS /api/v1/env-console/{id}/logs/stream`) and interactive web terminal (`WS /api/v1/env-console/{id}/terminal`). See [Agent Environment Console](../agents/agent_environments/agent_env_console.md).
**Why a dedicated block:** these are **WebSocket-only** routes (registered with `@router.websocket(...)` — there is no HTTP `GET` at these paths; the REST logs *snapshot* lives at the separate `GET /api/v1/environments/{id}/logs`). The generic `/api/` block proxies plain HTTP without the WebSocket upgrade directives, so the handshake arrives at the backend as an ordinary `GET`, matches no HTTP route, and Starlette returns **404**. Local dev works because the SPA connects straight to the backend via `VITE_API_URL`, bypassing the proxy. The routes live under a dedicated `/env-console` prefix (mounted on a separate `console_ws_router`, not the REST `/environments` router) precisely so this can be a simple prefix block that doesn't disable buffering on the REST environment endpoints.

**Special requirements:** WebSocket upgrade (`proxy_http_version 1.1`, `Upgrade`/`Connection` headers), `proxy_buffering off`, `proxy_cache off`, and a long `proxy_read_timeout`/`proxy_send_timeout` (3600s) so logs-follow and the terminal (env-core idle watchdog up to 900s) aren't cut mid-stream. Declare it before the generic `/api/` block.

```nginx
location /api/v1/env-console/ {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

**Auth note:** browsers cannot set an `Authorization` header on a WebSocket handshake, so these routes accept the platform JWT via the `?token=` query param (the dep also accepts `Authorization: Bearer` for non-browser callers). Ensure the proxy does **not** strip the query string.

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

### `/.well-known/cinna-app`

**Feature:** Cinna Mobile instance discovery (parallel `/app-auth` surface) — see [Desktop App Authentication](../application/desktop_auth/desktop_auth.md).
**Why at origin root:** Same rationale as `cinna-desktop` — the Cinna Mobile app fetches `https://{instance}/.well-known/cinna-app` before login to learn the `/app-auth` OAuth URLs, with no knowledge of API prefixes yet.
**Upstream:** backend.

### `/agent-hooks/`

**Feature:** Agent Webhooks public execution endpoint — see [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md).
**Why not under `/api/`:** Mounted at the app root (no `/api/v1` prefix) to match the task-trigger `/hooks/` convention and produce short, shareable URLs (`{host}/agent-hooks/{webhook_id}`).
**Upstream:** backend.

Note: `/hooks/` (task triggers) carries the same requirement. Both paths must appear in the production reverse proxy config. In local docker-compose mode requests reach the backend directly via `VITE_API_URL`, so no `frontend/nginx.conf` block is needed.

### `/agent-start`

**Feature:** Local Agent Kit public entrypoint — see [Local Agent Kit](../application/local_agent_kit/local_agent_kit.md).
**Why at origin root:** `/agent-start` is the URL a user pastes into a coding assistant ("read `https://{instance}/agent-start` and help me start making my agents"). It has to be short and memorable, and it is read by callers that know nothing about the instance — including a plain browser, which gets an HTML landing page. Without this block the SPA shell answers it and the assistant reads an empty React page instead of the kit.
**Match:** `location ~ ^/agent-start(/|$)` — a regex, not the prefix form. `/agent-start`, `/agent-start/`, `/agent-start/kit.tar.gz` and `/agent-start/kit/<path>` all reach the backend, and a future SPA route such as `/startup` does not (an nginx prefix match is on the raw string, so `location /agent-start` would swallow it).
**Upstream:** backend. Unauthenticated and read-only by design; responses are `Cache-Control: public, max-age=300` and identical for every caller, so an intermediary cache in front is fine.

**Local dev (Vite):** the Vite dev server has no nginx in front, so `frontend/vite.config.ts` carries the same rule as a `server.proxy` entry (`^/agent-start(/|$)` → `VITE_API_URL`). Without it `http://localhost:5173/agent-start` falls through to the SPA and 404s.

**`/api/agent-start` alias:** the same router is mounted a second time under `/api/agent-start`, which the universal `/api/` block above already proxies. Every link *inside* the kit points at the alias, so an instance whose reverse proxy was never given a `/agent-start` block still serves a fully working kit — only the pretty URL is affected. Adding the block is still recommended: the pasteable prompt is the feature's entry point.

### `/`

**Feature:** frontend SPA. `try_files $uri $uri/ /index.html` to support client-side routing.
**Upstream:** static files (served by nginx itself, no proxy).

## Configuration Files

- `frontend/nginx.conf` — in-container nginx config baked into the frontend Docker image. Serves the SPA and proxies `/api/`, the two WebSocket/SSE-upgrade blocks (`/api/v1/cli/`, `/api/v1/env-console/`) that must precede it, the `.well-known/*` URIs above, and `/agent-start`. Kept in parity with the production reverse proxy so the frontend container can act as the entry point in local docker-compose. (When the SPA talks to the backend directly via `VITE_API_URL`, these blocks are bypassed — but they must still match prod for correctness.)
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
- [Agent Environment Console](../agents/agent_environments/agent_env_console.md) — uses `/api/v1/env-console/` (`WS .../{id}/terminal` + `WS .../{id}/logs/stream`, web terminal + live logs follow)
- [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md) — uses `/agent-hooks/` (public webhook execution, no JWT)
- [Task Triggers](../application/input_tasks/task_triggers.md) — uses `/hooks/` (same pattern as agent webhooks, public webhook execution, no JWT)
- [Local Agent Kit](../application/local_agent_kit/local_agent_kit.md) — uses `/agent-start` (public kit entrypoint, no auth) with the `/api/agent-start` alias as the no-proxy-change fallback
