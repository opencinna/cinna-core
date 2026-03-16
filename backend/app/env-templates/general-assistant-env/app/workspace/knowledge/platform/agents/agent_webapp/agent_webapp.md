# Agent Webapp

## Purpose

Enable agents to build and serve lightweight data dashboards (HTML/CSS/JS) to users via shareable URLs. The webapp lives in the agent's workspace, is served through the backend (proxied from the running container), and supports dynamic data endpoints backed by Python scripts inside the agent environment.

Primary use case: visually appealing status reports with tables, charts, and filters connected to agent-managed databases and scripts. Users get a live, refreshable view of agent-processed data without needing to open a chat session.

## Core Concepts

### Single Webapp Per Agent

One agent = one webapp. The app lives at `/app/workspace/webapp/` inside the agent environment. If the user needs multiple dashboards, the agent builds a multi-page app with its own navigation.

### Feature Toggle

The webapp feature is **disabled by default** per agent. The owner explicitly enables it via agent settings (`webapp_enabled`). When disabled:
- Agent-env webapp endpoints return 404
- "Web App" menu item hidden in environment panel
- Webapp share creation blocked
- Building prompt reference still available (agent can create files, but they won't be served)

### Workspace Convention

```
/app/workspace/webapp/
├── index.html                 # Entry point (required)
├── assets/                    # CSS, JS, images
├── pages/                     # Additional HTML pages (optional)
├── data/                      # Pre-computed JSON data (optional)
└── api/                       # Python data endpoint scripts
    ├── get_sales.py           # Returns JSON to stdout
    └── README.md              # Documents available endpoints
```

The `webapp/` directory is preserved across rebuilds and suspensions (workspace volume). It is included in environment cloning and syncing operations.

### Static File Serving

All webapp content is served from the **running container** via dedicated agent-env endpoints. The backend proxies requests through the Docker adapter:

```
Browser -> Backend API -> Docker Adapter -> Agent-Env HTTP -> /app/workspace/webapp/{path}
```

Static files include caching headers (ETag, Last-Modified, Cache-Control: no-cache). Conditional requests (If-Modified-Since, If-None-Match) are passed through, returning 304 when content hasn't changed.

### Dynamic Data Endpoints

For interactive dashboards with filters, pagination, and real-time queries, the webapp's JavaScript calls data API endpoints. These execute Python scripts inside the container:

```
Browser JS -> Backend API -> Docker Adapter -> Agent-Env POST /webapp/api/{endpoint}
                                                -> runs: python webapp/api/{endpoint}.py
                                                -> params piped via stdin
                                                -> JSON stdout -> response
```

Data scripts:
- Receive parameters as JSON via stdin
- Print JSON result to stdout
- Have access to everything in the workspace (databases, credentials, other scripts)
- Run with a default timeout of 60s, configurable per-request up to 300s
- Responses always include `Cache-Control: no-store`

## User Stories

### Owner Enables and Previews Webapp

1. Owner navigates to agent settings (Environments tab)
2. Toggles "Web App" switch to enable the feature
3. In a chat session, asks the agent to build a dashboard
4. Agent creates files in `/app/workspace/webapp/`
5. Owner clicks "Web App" link in environment panel dropdown (opens in new tab)
6. Owner preview URL: `GET /api/v1/agents/{agent_id}/webapp/` serves index.html

### Owner Shares Webapp

1. Owner navigates to agent Integrations tab
2. Under "Web App Sharing", clicks "Create Share Link"
3. Configures: label (optional), expiration (1h/24h/7d/30d/never), allow data API toggle, require security code toggle
4. System generates a share URL with a random token; if security code is enabled, a 4-digit code is also generated
5. Owner copies the URL and/or embed snippet to share

### External User Accesses Shared Webapp

1. User opens the share URL: `/webapp/{token}`
2. Frontend fetches `/webapp-share/{token}/info` to check validity and code requirement
3. If no security code is required, the webapp loads directly (default behavior)
4. If security code is required, user enters the 4-digit code
5. Frontend authenticates via `/webapp-share/{token}/auth` and receives a short-lived JWT
6. Frontend renders a full-page iframe pointing to `/api/v1/webapp/{token}/`
7. If environment is not running, backend returns a loading page with polling that auto-activates the env
8. Once env is ready, the loading page reloads to serve actual webapp content

### Webapp Keeps Environment Alive

1. Webapp requests (static files and data API) update `last_activity_at` on the environment
2. This prevents the suspension scheduler from stopping an env with active webapp viewers
3. The existing `inactivity_period_limit` setting applies equally to webapp traffic

### Interface Configuration

Per-agent configuration that controls how the webapp is displayed to viewers across all share links. Accessible via the "Interface" button on the Web App integration card in the Integrations tab.

Available options:

- **Show Header** (default: on) — shows or hides the header bar at the top of the shared webapp page. The header displays the agent name and provides visual framing for the webapp. When hidden, the webapp renders full-screen without any chrome.
- **Chat Mode** (default: disabled) — controls whether webapp viewers can chat with the agent via an embedded chat widget. Three options: Disabled (no chat), Conversation (compact mode for interacting with existing UI), Building (full mode for creating/modifying widgets). See [Webapp Chat Widget](webapp_chat.md) for details.

These settings apply globally to all share links for the agent — there is no per-share override. The configuration is created with default values on first access (no manual setup required).

## Business Rules

### Webapp Shares

- Each share has a unique token (32 bytes, URL-safe)
- By default, shares are accessible without a security code — anyone with the link can view the webapp
- Owner can optionally enable a 4-digit security code when creating or editing a share (`require_security_code`)
- Owner can remove the security code requirement from an existing share (`remove_security_code`), making it accessible to anyone with the link
- Security code is encrypted at rest (Fernet)
- Wrong code increments `failed_code_attempts`; after 3 failures the share is blocked
- Owner can unblock by setting a new security code (resets attempts and blocked flag)
- Shares can be deactivated (`is_active = false`) without deleting
- Shares can have an expiration time (`expires_at`)
- The `allow_data_api` flag controls whether the data API is accessible through this share
- JWT issued on auth has `role: "webapp-viewer"`, 24h max lifetime (or share expiry, whichever is sooner)

### Access Levels

| Access type | Static files | Data API | Requires env running |
|---|---|---|---|
| Owner (regular auth) | Yes | Yes | Yes |
| Webapp share (token, no code) | Yes | If `allow_data_api` | Yes (auto-activate) |
| Webapp share (token + code) | Yes | If `allow_data_api` | Yes (auto-activate) |
| No token | No | No | N/A |

### Error Pages

When the webapp is accessed via a share URL (rendered inside an iframe), error responses must be styled HTML pages rather than raw JSON, since JSON would render as a blank screen. The backend returns self-contained error HTML for:
- **Webapp not built** — no `index.html` exists (404). Shows "Web App Not Built Yet" with retry button.
- **Size limit exceeded** — webapp directory over 100MB (413). Shows "Web App Too Large" with contact-owner message.
- **Generic errors** — adapter failures (500). Shows "Web App Unavailable" with retry button.

The `_status` polling endpoint also returns `"status": "error"` (not `"running"`) when `has_index` is false, so the loading page stops polling and shows the error with a retry button.

Owner preview routes (opened in a new tab, not iframe) return standard JSON error responses.

### Size Limit

- Default: 100MB for the entire `webapp/` directory
- Checked on index.html requests (entry point) via the webapp status endpoint
- Returns styled HTML error page (413) for public access, JSON for owner preview

### Auto-Activation

When a shared webapp URL is accessed and the environment is suspended:
- Backend returns a self-contained HTML loading page
- Loading page polls `/_status` every 2s
- Progressive steps: "Waking up the agent..." -> "Loading the app..." -> "Ready"
- Auto-activation is triggered for suspended environments
- Once ready, the loading page reloads to serve actual content
- Times out after 120s with an error message and retry button

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Browser                                                             │
│  Webapp (HTML/CSS/JS)                                               │
│    ├── Static assets:  GET /api/v1/webapp/{token}/{path}            │
│    └── Data requests:  POST /api/v1/webapp/{token}/api/{endpoint}   │
└──────────┬──────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Backend (FastAPI)                                                    │
│  ├── Owner preview routes:  /agents/{agent_id}/webapp/*             │
│  ├── Share CRUD routes:     /agents/{agent_id}/webapp-shares/*      │
│  ├── Public auth routes:    /webapp-share/{token}/*                 │
│  └── Public serving routes: /webapp/{token}/*                       │
│       ├── Validate token / owner auth                               │
│       ├── Check webapp_enabled                                      │
│       ├── Update last_activity_at (keep-alive)                      │
│       └── Proxy to agent-env                                        │
└──────────┬──────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Agent Environment (Docker Container)                                │
│  /app/workspace/webapp/          <- static files                    │
│  /app/workspace/webapp/api/      <- Python data scripts             │
│  Agent-env endpoints:                                               │
│    GET  /webapp/{path}           <- serve static file + caching     │
│    POST /webapp/api/{endpoint}   <- execute data script             │
│    GET  /webapp/status           <- metadata (exists, size, files)  │
└─────────────────────────────────────────────────────────────────────┘
```

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** - webapp directory lives in workspace volume, preserved across rebuilds; `last_activity_at` updated by webapp traffic to prevent suspension
- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** - `webapp/` included in environment cloning/syncing alongside files, scripts, docs
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** - three new endpoints in agent-env FastAPI server for static files, data API, and status
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** - `WEBAPP_BUILDING.md` prompt template referenced from `BUILDING_AGENT.md`; agent reads it when user asks to build a webapp
- **[Agent Sharing / Guest Sharing](../agent_sharing/guest_sharing.md)** - webapp shares are independent from guest shares; both can coexist on the same agent
- **[Agent Schedulers](../agent_schedulers/agent_schedulers.md)** - scheduled tasks can refresh webapp data (SQLite DB or JSON files); no new scheduler feature needed
- **[Agent Commands](../agent_commands/agent_commands.md)** - `/webapp` command returns the first active share URL directly in chat

---

*Last updated: 2026-03-08*
