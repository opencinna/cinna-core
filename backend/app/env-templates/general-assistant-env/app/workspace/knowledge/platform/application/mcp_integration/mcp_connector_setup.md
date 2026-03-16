# MCP Connector — Setup & Configuration

## Overview

This guide covers setting up MCP connectors for use with external MCP clients (Claude Desktop, Cursor, etc.), including local development with tunnels and configuring MCP clients for full functionality (messaging, file uploads).

## Architecture

```
MCP Client (Claude Desktop / Cursor)
    │
    │  HTTPS (MCP protocol + OAuth)
    ▼
Public Endpoint (tunnel or production URL)
    │
    │  Forwards to backend
    ▼
Backend (FastAPI, port 8000)
    │
    │  OAuth /authorize redirects browser for consent
    ▼
Frontend (consent page)
```

---

## MCP Client Configuration

### Claude Desktop

1. Open **Settings** (gear icon) → **Connectors**
2. Click **Add Custom Connector**
3. Enter a name for the agent and paste the MCP Server URL copied from the app UI
4. Click **Connect** and follow the on-screen instructions (OAuth consent flow)

### Cursor

Settings → MCP → Add Server → paste the MCP Server URL.

### Google Antigravity

1. Open the **Editor** in Google Antigravity
2. In the **top-right corner**, click the **`...`** (three-dot menu) button
3. From the dropdown, select **MCP Servers**
4. In the **Manage MCPs** window that opens, click **View raw config**
5. Paste the following JSON (replace the `serverUrl` with your MCP Server URL):

```json
{
  "mcpServers": {
    "your-agent-name": {
      "serverUrl": "https://your-tunnel.a.pinggy.link/mcp/{connector-uuid}/mcp"
    }
  }
}
```

6. **Save** the config
7. Back in the **Manage MCPs** window, click the **Refresh** button
8. This triggers the **OAuth authorization flow** for the connector — complete the consent process in the browser
9. Once complete, the connector appears as **enabled** in the Manage MCPs window, with its available tools listed

### Connection Flow

When the MCP client connects, it will automatically:

1. Hit `/.well-known/oauth-protected-resource` on the connector endpoint
2. Discover the shared OAuth AS via `/.well-known/oauth-authorization-server`
3. Register a client via DCR (`POST /mcp/oauth/register`)
4. Open your browser for OAuth consent (`GET /mcp/oauth/authorize` → redirect to frontend)
5. After you log in and approve, exchange the auth code for tokens
6. List available tools: `send_message`, `get_file_upload_url`
7. Discover workspace resources: `workspace://tree` and folder templates (`workspace://files/{path}`, etc.)

---

## Enabling File Uploads (Claude Desktop)

The `get_file_upload_url` tool returns a CURL command that the MCP client executes to upload files to the agent's workspace. By default, Claude Desktop blocks outbound network requests from its internal execution environment.

To enable file uploads:

1. Open **Claude Desktop** → **Settings** (gear icon)
2. Go to the **Capabilities** menu
3. Ensure **Allow network egress** is turned **on**
4. In the **Domain allowlist** section, find **Additional allowed domains**
5. Add the domain of your MCP endpoint:
   - For local development with pinggy: add your pinggy domain, e.g. `ohsxrqilub.a.pinggy.link`
   - For production: add your production server domain, e.g. `mcp.yourapp.com`
6. **Start a new chat** — existing chats will not pick up the updated settings

Without this configuration, the CURL command returned by `get_file_upload_url` will fail with a `CONNECT tunnel failed, response 403` error because Claude Desktop's egress proxy blocks the connection.

### What Gets Uploaded

When a user calls `get_file_upload_url`, the tool returns a CURL command like:

```
curl -X POST "https://your-domain.example.com/mcp/{connector-id}/upload" \
  -H "Authorization: Bearer <temporary-jwt>" \
  -F "file=@filename.ext" \
  -F "workspace_path=uploads"
```

The temporary JWT is valid for 15 minutes and is scoped to the specific connector. The file is proxied through the backend to the agent environment's `/files/upload` endpoint, which places it in the workspace's `uploads/` directory.

---

## Workspace Resources

MCP clients can browse and read files from the agent's workspace using the standard MCP resources protocol. This is automatic — no additional configuration is needed beyond connecting the MCP client.

### Available Resources

Workspace files are listed dynamically in `resources/list`. Each file in an allowed folder appears as a concrete resource:

| URI Pattern | Folder |
|-------------|--------|
| `workspace://files/{path}` | Agent-created files |
| `workspace://uploads/{path}` | User-uploaded files |
| `workspace://scripts/{path}` | Script files |

### Security

Only safe folders are exposed. Sensitive folders (`credentials/`, `databases/`, `docs/`, `knowledge/`, `logs/`) are excluded. Individual file reads are capped at 10MB — larger files should use the file upload/download tools instead.

### How It Works

1. MCP client calls `resources/list` → gets a dynamic list of all workspace files (e.g., `workspace://files/report.csv`, `workspace://uploads/data.json`, etc.)
2. MCP client calls `resources/read("workspace://files/report.csv")` → gets file content (text or base64-encoded binary)
3. If the agent environment is not running, `resources/list` returns an empty list gracefully

Nested paths are supported: `workspace://scripts/subfolder/run.sh` works correctly.

---

## Local Development Setup

### Prerequisites

- Docker services running (`make up`)
- Migrations applied (`make migrate`)
- An agent created in the UI with an active environment
- SSH client installed (for pinggy tunnel)

### Why a Tunnel is Needed

MCP clients need a **public URL** to reach your backend for the OAuth flow and MCP protocol, but your backend runs on `localhost:8000`. We solve this with a [pinggy](https://pinggy.io) tunnel.

**Why the frontend doesn't need a tunnel:** The MCP client runs on the same machine as the browser. When the OAuth flow opens the browser for consent, it navigates to `http://localhost:5173` which is directly accessible. Only the MCP protocol endpoints and OAuth token exchange (called programmatically by the MCP client) need the public tunnel URL.

### Pinggy Account

The free pinggy tier generates a **random subdomain** that expires after a few hours. Each time the tunnel restarts, you get a new URL and must update your configuration.

Consider activating a **paid pinggy account** (or at least the 7-day free trial) to get a stable custom subdomain. This avoids having to reset and update the tunnel domain every time it expires, and saves time reconfiguring MCP clients.

### Step-by-Step

#### 1. Start the Tunnel

**Terminal 1:**

```bash
make mcp-tunnel
```

This starts a pinggy SSH tunnel and prints instructions. You'll see output like:

```
Starting pinggy tunnel for MCP...
1) Copy the HTTPS URL from the tunnel output
2) In another terminal, run:
   make mcp-set-url URL=https://YOUR-TUNNEL.a.free.pinggy.link

http://rndzx-xxx-xxx.a.free.pinggy.link
https://rndzx-xxx-xxx.a.free.pinggy.link
```

Copy the **HTTPS** URL. Keep this terminal open — the tunnel stays active as long as the SSH session runs.

#### 2. Set URL and Recreate Backend

**Terminal 2:**

```bash
make mcp-set-url URL=https://rndzx-xxx-xxx.a.free.pinggy.link
```

This single command does three things:
1. Updates `MCP_SERVER_BASE_URL` in `.env` (automatically appends `/mcp`)
2. Recreates the backend container with the new env var
3. Verifies the MCP OAuth endpoint is reachable through the tunnel

**Why `up -d` and not `restart`:** `docker compose restart` reuses the same container with its original env vars. `docker compose up -d` detects that `.env` has changed and recreates the container, picking up the new `MCP_SERVER_BASE_URL` value.

#### 3. Verify (optional manual check)

The `mcp-set-url` command already verifies, but you can also check manually:

```bash
curl -s https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/oauth/.well-known/oauth-authorization-server | python3 -m json.tool
```

Expected response:
```json
{
    "issuer": "https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/oauth",
    "authorization_endpoint": "https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/oauth/authorize",
    "token_endpoint": "https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/oauth/token",
    "registration_endpoint": "https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/oauth/register",
    ...
}
```

If you see this, the tunnel → backend → MCP OAuth chain is working.

#### 4. Create an MCP Connector

1. Open the app at `http://localhost:5173`
2. Navigate to your agent → **Integrations** tab
3. Click **Create MCP Connector**
4. Fill in: name, mode (conversation/building), optional allowed emails
5. Copy the **MCP Server URL** from the created connector

The URL will look like:
```
https://rndzx-xxx-xxx.a.free.pinggy.link/mcp/{connector-uuid}/mcp
```

#### 5. Configure the MCP Client

See the [MCP Client Configuration](#mcp-client-configuration) section above. Use the MCP Server URL copied in step 4 to add the connector in your MCP client.

For file upload support, also follow the [Enabling File Uploads](#enabling-file-uploads-claude-desktop) section to allowlist the tunnel domain.

#### 6. Test the Connection

Send a message through the MCP client. You should see the `send_message` and `get_file_upload_url` tools available in the client's tool list.

### When the Tunnel URL Changes

If the tunnel drops or you restart it (free tier), the URL changes. To recover:

1. Restart the tunnel: `make mcp-tunnel`
2. Copy the new HTTPS URL
3. Run: `make mcp-set-url URL=https://NEW-TUNNEL.a.free.pinggy.link`
4. **MCP connectors keep their UUIDs** — only the base URL prefix changes
5. Remove the old connector in your MCP client and re-add it with the updated URL
6. Update the allowed domain in Claude Desktop settings (if using file uploads)
7. The MCP client will need to re-authenticate (old tokens reference the old URL)

---

## What Needs a Tunnel vs. What Doesn't

| Component | Accessed via | Needs tunnel? |
|-----------|-------------|---------------|
| MCP protocol endpoints (`/mcp/{id}/mcp`) | MCP client (programmatic) | Yes |
| OAuth token exchange (`/mcp/oauth/token`) | MCP client (programmatic) | Yes |
| OAuth authorize (`/mcp/oauth/authorize`) | Browser redirect (started by MCP client) | Yes |
| File upload (`/mcp/{id}/upload`) | MCP client (CURL) | Yes |
| Consent page (`/oauth/mcp-consent`) | Browser (localhost:5173) | No |
| Consent API (`/api/v1/mcp/consent/{nonce}/approve`) | Frontend JS to localhost:8000 | No |
| App UI / Connector management | Browser (localhost:5173) | No |

Native MCP clients (Claude Desktop, Cursor) make server-to-server calls — no browser origin involved. Browser-based clients (MCP Inspector) send an `Origin` header; the backend allows any `localhost` origin automatically in local development (`ENVIRONMENT=local`).

---

## Local Testing with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is a browser-based debugging tool that lets you test the full MCP chain — OAuth, tool calls, resources, prompts — without needing Claude Desktop or Cursor.

### Starting the Inspector

```bash
make mcp-inspector
```

This launches the inspector UI (typically at `http://localhost:6274`).

### Connecting to a Connector

1. In the inspector's **left-hand sidebar**, set the transport type to **Streamable HTTP**
2. Paste the **MCP Server URL** copied from the connector in the app UI (e.g. `https://your-tunnel.a.free.pinggy.link/mcp/{connector-uuid}/mcp`)
3. Click **Connect**
4. The inspector opens a browser tab for OAuth consent — log in and approve
5. Once connected, use the inspector tabs to call tools, list resources, and test prompts

### Domain Must Match `MCP_SERVER_BASE_URL`

The MCP Server URL you paste into the inspector **must use the same domain** as the `MCP_SERVER_BASE_URL` configured in `.env`. During OAuth, the `resource` parameter (derived from the connector URL) is verified against registered tokens — if the domains don't match, token exchange will fail with a resource mismatch error.

For example, if your `.env` has:
```
MCP_SERVER_BASE_URL=https://abc123.a.free.pinggy.link/mcp
```

Then the connector URL in the inspector must also use `https://abc123.a.free.pinggy.link/mcp/...` — not `http://localhost:8000/mcp/...`.

### Testing Directly Against localhost (No Tunnel)

If you don't need a tunnel (e.g. testing tools/resources without external OAuth clients), you can point the inspector at `localhost` directly:

1. Set `MCP_SERVER_BASE_URL=http://localhost:8000/mcp` in `.env` and recreate the backend (`docker compose up -d backend`)
2. In the inspector, paste `http://localhost:8000/mcp/{connector-uuid}/mcp`
3. Connect and complete OAuth as usual (consent page at `localhost:5173`)

This avoids tunnel setup entirely, but note that native MCP clients (Claude Desktop, Cursor) won't be able to connect since they need a public URL.

---

## `MCP_SERVER_BASE_URL` Format

```
MCP_SERVER_BASE_URL=https://{tunnel-or-production-host}/mcp
```

- Must include the `/mcp` suffix (matches the backend mount point)
- No trailing slash
- All MCP-facing URLs are derived from this single value:
  - MCP Server URL (user copies): `{MCP_SERVER_BASE_URL}/{connector_id}/mcp`
  - File Upload URL: `{MCP_SERVER_BASE_URL}/{connector_id}/upload`
  - OAuth AS metadata: `{MCP_SERVER_BASE_URL}/oauth/.well-known/oauth-authorization-server`
  - Token endpoint: `{MCP_SERVER_BASE_URL}/oauth/token`

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| 404 on all MCP endpoints | Tunnel URL changed or `MCP_SERVER_BASE_URL` empty | Update `.env`, recreate backend |
| Consent page won't load | Frontend not running | `make dev-front` or `docker compose up -d frontend` |
| "Connector not found or inactive" | Connector deactivated or deleted | Check connector status in the Integrations tab |
| OAuth flow opens browser but hangs | Tunnel SSH session closed | Restart tunnel, update config, recreate backend |
| "Invalid resource URL" on DCR | `MCP_SERVER_BASE_URL` format wrong | Must end with `/mcp`, no trailing slash |
| Token exchange fails | URL mismatch between DCR and token request | Ensure MCP_SERVER_BASE_URL matches the current tunnel |
| `send_message` returns error | Agent environment not running | Start the agent environment from the UI |
| Repeated "unauthorized" after re-auth | Stale tokens from old tunnel URL | Delete MCP client's cached credentials and reconnect |
| `CONNECT tunnel failed, response 403` on file upload | Claude Desktop blocks outbound requests | Allowlist the MCP domain in Claude Desktop settings (see [Enabling File Uploads](#enabling-file-uploads-claude-desktop)) |
| File upload 401 | Upload token expired (15-min lifetime) | Request a new upload URL via `get_file_upload_url` |
| File upload 503 | Agent environment not running | Start the environment from the UI |

---

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make mcp-tunnel` | Start pinggy tunnel for MCP testing (keep terminal open) |
| `make mcp-set-url URL=https://xxx.pinggy.link` | Update `.env`, recreate backend, verify endpoint |
| `make mcp-inspector` | Launch MCP Inspector for local debugging and testing |
| `make dev-tunnel` | Generic tunnel (same SSH command, no MCP instructions) |

## Quick Reference

```bash
# Terminal 1: Start tunnel (keep open)
make mcp-tunnel
# → Copy the HTTPS URL

# Terminal 2: Set URL + recreate backend (one command)
make mcp-set-url URL=https://YOUR-TUNNEL.a.free.pinggy.link

# Then:
# 1. Create MCP connector in UI (localhost:5173 → agent → Integrations)
# 2. Copy MCP Server URL
# 3. Claude Desktop: Settings → Connectors → Add Custom Connector → paste URL → Connect
# 4. Allowlist the tunnel domain in Claude Desktop → Settings → Capabilities
# 5. Start a new chat in Claude Desktop
# 6. MCP client handles OAuth automatically
```

---

## Related Documentation

- `docs/application/mcp_integration/agent_mcp_architecture.md` — Architecture overview
- `docs/application/mcp_integration/agent_mcp_connector.md` — Implementation reference
