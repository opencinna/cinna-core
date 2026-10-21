---
feature: nginx_setup
domain: infrastructure
one_liner: "Reverse-proxy setup: one unified production config routes every backend path, plus the frontend container nginx and the rules for adding a backend path."
primary_label: reference
docs:
  data volumes: data_volumes.md
---
# Nginx Setup

## Purpose

The Cinna platform relies on nginx in two places:

- **Production reverse proxy**: terminates TLS and routes each request either to the backend or to the frontend SPA. Its Cinna part is the unified config in [Production setup](#production-setup).
- **Frontend container nginx** (`frontend/nginx.conf`): serves the static SPA build and proxies the backend paths, so the frontend container can be the entry point in local docker-compose.

A backend path missing from the proxy falls through to the SPA's `try_files` and answers `200 text/html` with the SPA shell. The failure then looks like a frontend bug (a blank viewer, a CORS error, a JSON parse error in a client) rather than a routing gap.

## Production setup

Paste the config below into the site's `server { ... }` block, replacing any per-path Cinna blocks from older setups, and set the backend address in both `proxy_pass` lines. TLS, `root`, `index` and server-level headers stay in the operator's server config.

```nginx
# ── Backend: every path the backend owns, one block ──────────────────────────
#   api/            REST API (incl. /api/agent-start, /api/cli-setup, /api/v1/hooks)
#   mcp/            MCP Streamable HTTP (SSE) + MCP OAuth
#   ws/             Socket.IO realtime events (WebSocket)
#   acp/            ACP connectors (WebSocket)
#   a/              artifact content (sandboxed; backend sets CSP + CORS)
#   agent-hooks/    agent webhooks (public)
#   agent-start     Local Agent Kit pasteable URL (also /agent-start/<path>)
#   .well-known/... OAuth metadata (RFC 9728 / RFC 8414), desktop/mobile discovery
location ~ ^/(api/|mcp/|ws/|acp/|a/|agent-hooks/|agent-start(/|$)|\.well-known/(oauth-protected-resource|oauth-authorization-server|cinna-desktop|cinna-app)) {
    proxy_pass http://localhost:8000;
    proxy_http_version 1.1;

    # WebSocket when the client asks for an upgrade, plain HTTP otherwise.
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE (MCP, CLI exec), the CLI sync tunnel, the env console and artifact
    # range requests must stream and must not be cut while idle.
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    # Largest upload (file uploads, UPLOAD_MAX_FILE_SIZE_MB). Smaller per-route
    # limits (skills 17 MiB, artifacts 55 MiB) are enforced by the backend.
    client_max_body_size 100m;
}

# ── Desktop agents: git smart HTTP (clone / pull / push) ─────────────────────
# Separate only because pushes exceed the general body limit.
# Values match GIT_MAX_PUSH_BYTES and GIT_HTTP_TIMEOUT_SECONDS.
location /git/ {
    proxy_pass http://localhost:8000;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
    client_max_body_size 256m;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

# ── Frontend SPA: everything else ────────────────────────────────────────────
location / {
    try_files $uri $uri/ /index.html;
}
```

It holds three locations:

| Location | Routes | Why it is separate |
|----------|--------|--------------------|
| `~ ^/(api/\|mcp/\|ws/\|acp/\|a/\|agent-hooks/\|agent-start(/\|$)\|\.well-known/(...))` | every backend path except git | one transport profile fits all of them (below) |
| `/git/` | desktop agent git smart HTTP | pushes need a 256 MiB body and no request buffering |
| `/` | the SPA, `try_files $uri $uri/ /index.html` | static files |

**Backend block transport profile.** It is tuned to the most demanding route it carries, which is harmless for the rest:

- `proxy_http_version 1.1`, `Upgrade $http_upgrade`, `Connection $http_connection`: WebSocket handshakes (`/ws/`, `/acp/`, CLI sync tunnel, env console) are upgraded, and plain requests stay plain. Passing the client's own `Connection` header avoids a `map`, which would have to live in the `http` context (awkward on managed hosts such as Forge).
- `proxy_buffering off`, `proxy_cache off`: SSE (MCP, CLI exec) and artifact range responses stream. Artifact content must not be cached at the proxy, because the view capability is re-checked on every request.
- `proxy_read_timeout`/`proxy_send_timeout 3600s`: long-lived tunnels and terminals are not cut while idle. The trade-off is that a hung API request is held up to an hour instead of 60 s.
- `client_max_body_size 100m`, the largest upload (`UPLOAD_MAX_FILE_SIZE_MB`). Smaller per-route caps are enforced by the backend with a JSON 413: skill publish 17 MiB, artifacts `ARTIFACT_MAX_REVISION_BYTES + ARTIFACT_UPLOAD_OVERHEAD_BYTES` (55 MiB).
- Forwarded headers: `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`.
- No `add_header`. The backend sets CSP, CORS and framing headers itself (see [Artifact content](#artifact-content-a)).

**Placement.** nginx picks a matching regex location over prefix ones, so the backend block always beats `location /`. Among regex locations the first match in file order wins, so the config must sit **above** any other regex location in the server block. Otherwise an earlier `location ~* \.(js|css)$` cache rule would capture `/a/v/<cap>/app.js`, for example. A `location ~ /\.(?!well-known)` dotfile deny rule placed below keeps working for non-backend paths.

**Upgrades.** A Cinna release that adds an origin-root backend path changes the regex here. Re-apply it on upgrade.

**Check after reload.** Every path must print `0` (the backend answered); `1` means the SPA shell answered:

```bash
H=https://your-host
for p in /api/v1/utils/health-check/ /a/v/x/x /git/x.git/info/refs \
         /agent-start /agent-hooks/x /mcp/x /.well-known/cinna-desktop \
         /.well-known/oauth-authorization-server; do
  printf '%-42s ' "$p"; curl -s "$H$p" | grep -c 'id="root"'
done
```

## Backend paths

Everything outside these paths belongs to the SPA. Task-trigger webhooks (`/api/v1/hooks/{id}`), the CLI setup bootstrap (`/api/cli-setup/...`), the OpenAPI schema and the kit alias `/api/agent-start` all sit under `api/`.

| Path | Feature | Why at the origin root |
|------|---------|------------------------|
| `/api/` | all REST API traffic, incl. the CLI and env-console WebSockets/SSE | — |
| `/mcp/` | MCP Streamable HTTP and MCP OAuth: [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md), [App MCP Server](../application/app_mcp_server/app_mcp_server_tech.md) | URLs held by external MCP clients |
| `/ws/` | Socket.IO realtime events: [Realtime Events](../application/realtime_events/event_bus_system.md) | Socket.IO mount |
| `/acp/` | ACP connectors (WebSocket, `ACP_SERVER_BASE_URL`) | URLs held by ACP clients |
| `/a/` | artifact content: [Artifacts](../application/artifacts/artifacts.md) | content origin, see below |
| `/git/` | desktop agent git: [Desktop Agents in the Catalog](../application/catalog/desktop_agents.md) | clone URLs held by desktops, no `/api/` alias |
| `/agent-hooks/` | agent webhooks: [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md) | short URLs held by external systems |
| `/agent-start` | Local Agent Kit: [Local Agent Kit](../application/local_agent_kit/local_agent_kit.md) | the pasteable URL |
| `/.well-known/oauth-protected-resource` | RFC 9728 metadata for MCP OAuth | RFC requires the root |
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata for MCP OAuth | RFC requires the root |
| `/.well-known/cinna-desktop` | Cinna Desktop discovery: [Desktop App Authentication](../application/desktop_auth/desktop_auth.md) | fetched before login, no API prefix known yet |
| `/.well-known/cinna-app` | Cinna Mobile discovery (`/app-auth` surface) | same as `cinna-desktop` |

### Cinna CLI (`/api/v1/cli/`)

`WS /api/v1/cli/agents/{agent_id}/sync-stream` (Mutagen tunnel) and `POST /api/v1/cli/agents/{agent_id}/exec` (SSE) need the WebSocket upgrade, unbuffered responses and the 3600 s timeouts. The unified backend block provides all three. Older setups need a dedicated `location /api/v1/cli/` block before `/api/`. See [Cinna CLI Integration](../application/cinna_cli_integration/cinna_cli_integration.md).

### Agent Environment Console (`/api/v1/env-console/`)

`WS /api/v1/env-console/{id}/terminal` and `WS .../{id}/logs/stream` are WebSocket-only routes. Behind a block without upgrade headers the handshake arrives as a plain `GET` and Starlette returns 404. Browsers cannot set `Authorization` on a WebSocket handshake, so the JWT travels in `?token=`. The proxy must not strip the query string. The routes sit under their own `/env-console` prefix so that a per-path setup can unbuffer them without unbuffering the REST environment routes. See [Agent Environment Console](../agents/agent_environments/agent_env_console.md).

### Artifact content (`/a/`)

Paths `/a/v/<cap>/<path>` and `/a/z/<cap>`, all methods. The backend's `ArtifactContentMiddleware` answers the `OPTIONS` preflight itself, and an unknown `/a/...` path gets its 404 with sandbox headers. The pattern keeps the trailing slash, so the SPA pages `/artifacts` and `/artifact/<id>` are not captured.

- **Which host.** The host of `artifacts_content_origin`: `ARTIFACTS_CONTENT_ORIGIN`, else `BACKEND_BASE_URL`, else `FRONTEND_HOST`. With a split API host the API host's proxy must route `/a/`. See [Artifacts tech: Deployment](../application/artifacts/artifacts_tech.md#deployment).
- **Headers.** Do not add or override CSP, CORS or `X-Frame-Options`. The backend sets the sandbox CSP, `frame-ancestors <FRONTEND_HOST origin>` and `Access-Control-Allow-Origin: *`. nginx inheritance rule: a location without its own `add_header` inherits all of the `server` block's. A server-wide `X-Frame-Options SAMEORIGIN` is harmless while content shares the SPA's host, and blocks the viewer iframe once the content origin is another host. In that case move such headers into `location /`.
- **Logs.** The capability is part of the URL path, so it appears in access logs. Treat them as sensitive (caps are short-lived and re-checked per request).
- **Rule.** Never add a cookie-authenticated API with `SameSite=None`. Artifact content is credential-free by design.
- **Symptom when not routed.** The viewer is blank, and the console shows `Cross-Origin Request Blocked ... /assets/index-<hash>.js (Reason: CORS header 'Access-Control-Allow-Origin' missing)` plus `Module source URI is not allowed in this document` at `index.html:13`. The SPA shell was served into the sandboxed iframe, whose origin is opaque (`null`), so its `crossorigin` module script fails CORS. The markdown, CSV and PDF viewers fail too, because they fetch `/a/v/...` and get HTML back.
- **Verify.** `curl -si https://<content host>/a/v/invalid/x` returns `404` with `content-security-policy: sandbox ...` and `access-control-allow-origin: *`.

### Local Agent Kit (`/agent-start`)

`/agent-start` is what a user pastes into a coding assistant. A plain browser gets an HTML landing page. The pattern `agent-start(/|$)` matches `/agent-start`, `/agent-start/` and `/agent-start/<path>`, but not a future SPA route such as `/startup` or `/agent-starter`. A plain prefix `agent-start` would match those, because nginx prefix matching is on the raw string. Responses are `Cache-Control: public, max-age=300` and the same for every caller.

The router is mounted a second time under `/api/agent-start`, and every link inside the kit points at that alias. An instance whose proxy lacks the `/agent-start` route still serves a working kit; only the pretty URL fails.

**Local dev (Vite).** The Vite dev server has no nginx in front, so `frontend/vite.config.ts` carries `server.proxy` entries for `^/agent-start(/|$)` and `^/a/` → `VITE_API_URL`.

### Git smart HTTP (`/git/`)

`/git/<repo_id>.git/...` clones and pushes stream packs both ways. The block uses `proxy_buffering off`, `proxy_request_buffering off`, `client_max_body_size 256m` (= `GIT_MAX_PUSH_BYTES`) and 600 s timeouts (= `GIT_HTTP_TIMEOUT_SECONDS`). There is deliberately no `/api/` alias.

## Frontend container nginx

`frontend/nginx.conf` is baked into the frontend image. It keeps per-path blocks with `set $upstream http://backend:8000; proxy_pass $upstream;` and Docker's resolver, so nginx starts even before the backend container resolves. It must route the same backend paths as the production config, apart from those local docker-compose never sends through it (`/mcp/`, `/ws/`, `/acp/`, `/agent-hooks/`; the SPA reaches the backend directly via `VITE_API_URL`). `frontend/nginx-backend-not-found.conf` is a mountable snippet that returns 404 for `/api`, `/docs`, `/redoc` when the frontend container runs without a backend.

## Adding a backend path

1. **Prefer `/api/`.** A route under `/api/` needs no proxy change on any deployment. Add an origin-root path only when a spec requires it (`/.well-known/*`) or the URL is user-facing and has to be short (pasted, or stored by external systems).
2. For an origin-root path:
   - add it to the regex in [Production setup](#production-setup) (and to the comment list above it)
   - add a block to `frontend/nginx.conf`, plus a `server.proxy` entry in `frontend/vite.config.ts` if it is hit from the browser in local dev
   - add a row to [Backend paths](#backend-paths)
   - add it to the check loop under [Production setup](#production-setup)
3. Prefer serving the route under an `/api/...` alias as well (as `/agent-start` does), so a deployment that has not re-copied the file degrades to "the short URL fails" rather than "the feature fails".
4. Say in the release notes that operators must re-apply the production config.
5. A route whose needs exceed the backend block (bigger bodies, request streaming) gets its own location in the config, like `/git/`.

## Integration Points

- [Artifacts](../application/artifacts/artifacts.md): `/a/` and the backend-enforced upload cap
- [MCP Integration](../application/mcp_integration/agent_mcp_architecture.md): `/mcp/`, `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`
- [App MCP Server](../application/app_mcp_server/app_mcp_server.md): shares `/mcp/` with per-agent MCP servers
- [Desktop App Authentication](../application/desktop_auth/desktop_auth.md): `/.well-known/cinna-desktop`, `/.well-known/cinna-app`
- [Desktop Agents in the Catalog](../application/catalog/desktop_agents.md): `/git/`
- [Realtime Events](../application/realtime_events/event_bus_system.md): `/ws/`
- [Cinna CLI Integration](../application/cinna_cli_integration/cinna_cli_integration.md): `/api/v1/cli/` (WebSocket sync tunnel, SSE exec stream)
- [Agent Environment Console](../agents/agent_environments/agent_env_console.md): `/api/v1/env-console/` (WebSocket terminal and logs follow)
- [Agent Webhooks](../agents/agent_webhooks/agent_webhooks.md): `/agent-hooks/` (public, no JWT)
- [Task Triggers](../application/input_tasks/task_triggers.md): `/api/v1/hooks/` (public, no JWT; under `/api/`)
- [Local Agent Kit](../application/local_agent_kit/local_agent_kit.md): `/agent-start`, with the `/api/agent-start` alias as the fallback
- [Data Volumes](data_volumes.md): storage mounts behind these routes
