# User Dashboards

## Overview

User Dashboards provide a customizable grid-based control panel where users can monitor multiple agents at a glance. Each dashboard is a named collection of **blocks**, where each block is tied to a specific agent and renders one of three view types: embedded webapp, latest session summary, or latest tasks list.

Dashboards are intentionally **workspace-independent** — any agent a user owns can be added to any dashboard regardless of which workspace it belongs to.

---

## User Flows

### Creating a Dashboard

1. Open the `SidebarDashboardSwitcher` dropdown in the sidebar footer and click "Manage Dashboards", or navigate directly to `/dashboards`.
2. Click "New Dashboard", enter a name (required) and optional description.
3. Dashboard is created and user is redirected to the new (empty) grid view.

### Adding a Block

1. From the dashboard grid view, click "Edit Layout" in the page header to enter edit mode.
2. Click "+ Add Block" (appears in the header only while in edit mode).
3. Select an agent from the dropdown (all owned agents, no workspace filter).
4. Choose a view type from the dropdown: Latest Session, Latest Tasks, Web App, or Agent Env File.
   - 4a. (Agent Env File only) A file picker dropdown appears below, listing files from the agent's active environment workspace. Select the file to display.
5. Click "Add". The block appears at the next available grid position.

### Rearranging Blocks

1. Click "Edit Layout" in the page header to enter edit mode.
2. Drag blocks by the transparent overlay handle (`.drag-handle`, visible on hover) to reposition; drag the southeast resize handle to resize.
3. Layout saves automatically (debounced 300ms) via the bulk layout API.
4. Click "Lock Layout" to exit edit mode and prevent accidental changes.

### Editing a Block

1. Enter edit mode ("Edit Layout"). Block headers with the kebab (⋮) menu are only visible in edit mode.
2. Click the kebab menu on a block header → "Edit Block".
3. Modify custom title, show/hide border, or show/hide header. The agent and view type are fixed after creation.
4. For `agent_env_file` blocks, a file picker dropdown is shown to change the displayed file.
5. Click "Save Changes".

Prompt actions are managed separately via the kebab menu → "Edit Prompt Actions" (see below).

### Deleting a Block

1. Enter edit mode. Click the kebab menu → "Remove".
2. Inline confirmation prompt replaces block content.
3. Block is removed; grid reflows.

### Editing a Dashboard

1. Click the ⋮ dropdown menu in the page header (top-right).
2. Select "Edit Dashboard".
3. Modify the name or description.
4. Click "Save".

### Deleting a Dashboard

1. Click the ⋮ dropdown menu in the page header → "Delete Dashboard".
2. Confirmation dialog shows.
3. All blocks in the dashboard are removed (CASCADE). User is redirected to `/dashboards`.

Alternatively, dashboards can also be deleted from the Manage Dashboards page (`/dashboards`) via the kebab menu on each dashboard card.

### Opening Fullscreen

1. Click the ⋮ dropdown menu in the page header → "Open Fullscreen".
2. A new browser tab opens at `/dashboard-fullscreen/{id}` showing only the dashboard grid — no sidebar, no header.
3. Fullscreen view is read-only (no edit mode).

---

## Page Header Actions

The dashboard view page injects actions into the shared layout header (same pattern as the session page):

- **Left**: Dashboard icon + dashboard name
- **Right**: Action buttons + dropdown menu

```
[LayoutDashboard icon] Dashboard Name          [+ Add Block] [Edit Layout] [⋮]
                                                                            ├── Open Fullscreen
                                                                            ├── Edit Dashboard
                                                                            ├── ─────────────
                                                                            └── Delete Dashboard
```

- "Add Block" button is only visible when edit mode is active, positioned left of "Edit Layout".
- "Edit Layout" toggles between edit/lock states.
- The ⋮ dropdown follows the same pattern as the session page dropdown.

---

## Block Display Modes

Blocks have two visual modes:

### Regular Mode (default)
- **No header** by default — blocks show only their content (view area fills the entire block).
- **Header** — shown when `show_header` is enabled per block. Displays the same colored bar as edit mode (agent color dot, view type icon, block title) but without the kebab menu.
- **Border** — only shown when `show_border` is explicitly enabled per block.
- Content-only presentation for a clean monitoring experience.

### Edit Mode (when "Edit Layout" is active)
- **Header visible** — colored bar with agent color dot, view type icon, block title, and kebab menu (Edit / Remove).
- **Border always shown** — all blocks display a border to help visualize block boundaries during layout editing.
- Drag handles and resize handles are active.

---

## Block View Types

| View Type | Description |
|-----------|-------------|
| `latest_session` | Shows the most recent sessions for the agent in a scrollable list matching the sessions index page style: mode icon (wrench/message), title, and relative timestamp per row. Clicking a row navigates to that session. |
| `latest_tasks` | Shows the 5 most recent tasks with status color coding: violet=new, blue=running, amber=pending, red=error, green=completed. |
| `webapp` | Embeds the agent's web application in an iframe using the authenticated owner preview route. Only available when the agent has `webapp_enabled = true`. |
| `agent_env_file` | Displays the content of a workspace file from the agent's active environment (resolved automatically from `active_environment_id`). The user selects a file path from the file picker; no environment selector is needed. Supports CSV, Markdown, JSON, TXT, and LOG files with syntax-appropriate viewers; other file types render as plain text. File content is read directly from the local filesystem (no running container required for DockerEnvironmentAdapter). |

Block content auto-refreshes every 30 seconds.

---

## Prompt Actions

Dashboard blocks can have **prompt actions** — one-click buttons that appear when hovering over a block in view mode. Clicking a button sends the configured prompt text to the agent and shows the streaming response directly inside the block overlay, without navigating away from the dashboard.

### Configuration

1. Enter edit mode and open the block's kebab menu → "Edit".
2. In the "Prompt Actions" section at the bottom of the dialog, click "+ Add".
3. Fill in the **Prompt Text** (required) — the message to send to the agent.
4. Optionally add a **Button Label** — a short display name for the button. If left blank, a truncated version of the prompt text is shown.
5. Click "Save" on the action row to persist it immediately.
6. Repeat for additional actions. Click the trash icon to delete a saved action immediately.

### UX Flow (view mode)

1. User hovers over a block that has prompt actions configured.
2. Small pill buttons appear at the bottom of the block in a semi-transparent overlay bar.
3. A `MessageCircle` icon appears on the left side of the action bar:
   - **Muted/grey**: no active session yet for this block.
   - **Primary color**: an active session exists (persists across page refreshes).
   - **Clickable**: navigates to the full session chat page (`/session/{id}`).
5. User clicks a prompt action button:
   - The overlay resolves (or creates) a session for the block, then sends the prompt text immediately via `MessagesService.sendMessageStream()`.
   - For webapp blocks, page context (schema.org microdata + selected text from the iframe) is collected and attached to the message as `page_context`.
   - The button shows a `Loader2` spinner while the request is in flight.
6. While the agent is streaming a response, a compact `StreamingMessage` appears inside the overlay above the action buttons. If no streaming events have arrived yet, a "Processing..." indicator with a spinner is shown.
7. When the stream completes, the streaming display clears and the action buttons return to their normal state. The session indicator stays active (primary color).
8. The block auto-refreshes every 30 seconds, so completed sessions will appear in the Latest Session view.

### Session Persistence and Reuse

Each block reuses its most recent session rather than creating a new one every time:

- On **mount**, `PromptActionsOverlay` calls `GET /api/v1/dashboards/{id}/blocks/{block_id}/latest-session` to load any session active in the last 12 hours. If found, the session indicator turns active immediately (persists across page refreshes).
- On **action click**, the overlay calls the same endpoint first. If a recent session exists it is reused; if not, a new `conversation`-mode session is created tagged with `dashboard_block_id`.
- This means repeated prompt actions accumulate in a single ongoing conversation rather than spawning one session per click.

### Webapp Action Forwarding

For blocks with `view_type = "webapp"`, the overlay also listens for `webapp_action` events received on the WebSocket stream. These events are forwarded to the embedded iframe via `postMessage`, using the same pattern as the `WebappChatWidget`. This allows agent responses to trigger UI state changes in the webapp (e.g., highlighting a row, switching a view).

### Business Rules

- A block can have any number of prompt actions (no enforced cap beyond practical UI constraints).
- Prompt actions are only visible in view mode (never in edit mode).
- Each action stores: `prompt_text` (1–2000 characters), optional `label` (max 100 characters), and `sort_order`.
- While a prompt action response is streaming, all action buttons are disabled (no concurrent streams per block).
- Session state (active session ID + streaming status) is tracked in component memory; it persists across hover events but resets on full page reload. The session indicator is restored on mount via the latest-session API call.
- Prompt actions are cascade-deleted when their parent block is deleted.
- New sessions are always created in `conversation` mode, tagged with `dashboard_block_id`.
- The `dashboard_block_id` FK on `session` uses `ondelete="SET NULL"` — deleting the block sets the column to NULL rather than deleting the session.

---

## Webapp Iframe Authentication

The `webapp` view type embeds the agent's webapp in an iframe via the owner preview route (`/api/v1/agents/{id}/webapp/`). Since iframes cannot set Authorization headers, a multi-layered auth approach is used:

1. **Initial load**: Frontend appends `?token={jwt}` query parameter to the iframe URL.
2. **Cookie persistence**: On the first request with `?token=`, the backend sets an `httponly` cookie (`webapp_owner_token`) scoped to the agent's webapp path.
3. **Sub-resource requests**: Subsequent requests for JS, CSS, images from within the iframe use the cookie automatically (no query param needed).
4. **Data API calls**: POST requests to `/api/v1/agents/{id}/webapp/api/{endpoint}` from within the iframe also use the cookie.
5. **Token priority**: Authorization header > `?token=` query param > `webapp_owner_token` cookie.

---

## Sidebar Integration

The sidebar footer contains two stacked components: `SidebarDashboardSwitcher` (above) and `SidebarWorkspaceSwitcher` (below). They follow the same dropdown pattern.

`SidebarDashboardSwitcher` shows a button labeled with the active dashboard name, or "Dashboards" when not on a dashboard route. Clicking it opens a dropdown:

```
[Dashboard name or "Dashboards"]  (click to open dropdown)
  ├── [Dashboard A]      → /dashboards/{id}
  ├── [Dashboard B]      → /dashboards/{id}
  │   (all dashboards shown, no limit)
  ├── ---separator---
  └── Manage Dashboards  → /dashboards
```

A separate "Dashboard" link (Home icon) exists as a standard nav item in the main sidebar nav pointing to `/`.

The active dashboard is highlighted with an accent background and check icon in the dropdown.

---

## Business Rules

- **Max 10 dashboards** per user. The 11th creation attempt returns HTTP 409.
- **Max 20 blocks** per dashboard. The 21st addition attempt returns HTTP 409.
- **Ownership-based access**: Users can only read/write their own dashboards and blocks.
- **Agent access**: When adding a block, the agent must be owned by the current user. Shared agents are not currently eligible.
- **Webapp validation**: Blocks with `view_type = "webapp"` require the agent to have `webapp_enabled = true`. The backend validates this on block creation; if the agent disables webapp later, the block shows a placeholder.
- **Agent Env File config**: Blocks with `view_type = "agent_env_file"` store `config` with `file_path` (non-empty, no path traversal). The environment is resolved automatically from the agent's `active_environment_id` — no `env_id` is stored in the block config. Backend validates the agent has an active environment on creation.
- **CASCADE deletes**: Deleting an agent removes all blocks referencing it across all dashboards. Deleting a dashboard removes all its blocks.
- **No workspace scoping**: The `user_dashboard` table has no `user_workspace_id` column. Dashboards span all workspaces.

---

## Error States

| Condition | Display |
|-----------|---------|
| Agent deleted while dashboard is open | Block shows "Agent unavailable" placeholder |
| Webapp not enabled | Block shows "Web App not enabled for this agent" |
| Environment not running (webapp) | iframe shows error from the webapp serving layer |
| Max dashboards reached | "New Dashboard" button disabled with tooltip |
| Max blocks reached | "Add Block" button disabled with tooltip |
| Delete block fails | Toast error: "Failed to remove block. Please try again." |
| Prompt action send fails | Toast error: "Failed to send prompt action. Please try again." |
| File not found in workspace | Block shows "Failed to load file" with alert icon |
| Environment not running (agent_env_file fallback path) | Block shows "Failed to load file" (only when adapter does not support local file access and environment is stopped) |
| `file_path` missing from agent_env_file block config | Block shows "No file configured" placeholder |

---

## Integration Points

- **Sessions API**: `LatestSessionView` fetches `/api/v1/sessions/?agent_id={id}&limit=10&order_by=last_message_at&order_desc=true`
- **Tasks API**: `LatestTasksView` fetches `/api/v1/tasks/?limit=5`
- **Webapp serving**: `WebAppView` embeds `/api/v1/agents/{id}/webapp/?token={jwt}` as an authenticated iframe (see Webapp Iframe Authentication above)
- **Agent list**: "Add Block" dialog calls `/api/v1/agents/?limit=200` (no workspace filter) to populate the agent picker
- **Sidebar**: `SidebarDashboardSwitcher` queries `["userDashboards"]` for the dropdown list
- **Page header**: Dashboard view page uses `usePageHeader` hook to inject the dashboard name, action buttons, and dropdown menu into the shared layout header (same pattern as the session page)
- **Prompt actions — session reuse**: `GET /api/v1/dashboards/{id}/blocks/{block_id}/latest-session` returns the most recent session tagged to a block (within 12 hours), used on mount and before each action send
- **Prompt actions — message sending**: `MessagesService.sendMessageStream()` sends the prompt text to the resolved session; the WebSocket stream room `session_{id}_stream` is subscribed before the message is sent
- **Prompt actions — streaming**: `PromptActionsOverlay` subscribes to `stream_event` and `session_interaction_status_changed` via `eventService`; streaming display uses the shared `StreamingMessage` component in `compact` conversation mode
- **Webapp context**: `buildPageContext()` from `src/utils/webappContext.ts` collects page context from the webapp iframe via `postMessage`; same utility used by `WebappChatWidget`
- **Webapp action forwarding**: `webapp_action` stream events are forwarded to the embedded iframe via `postMessage` (same pattern as `WebappChatWidget`)
- **Chat interface aspect docs**: Full prompt actions behavior documented in **[Dashboard Prompt Actions](../chat_interface/dashboard_prompt_actions.md)** (business logic) and **[tech](../chat_interface/dashboard_prompt_actions_tech.md)**
- **Agent Env File — file listing**: `AddBlockDialog` and `EditBlockDialog` call `GET /api/v1/dashboards/{id}/blocks/{block_id}/env-files?subfolder=files` to list available workspace files for the file picker
- **Agent Env File — file streaming**: `AgentEnvFileView` calls `GET /api/v1/dashboards/{id}/blocks/{block_id}/env-file?path={filePath}` to stream workspace file content. Uses `refetchInterval: 30000` (React Query key: `["dashboardBlockEnvFile", dashboardId, blockId, filePath]`)
- **Agent Env File — environment resolution**: The backend resolves the agent's active environment automatically via `agent.active_environment_id` — no environment selector is needed in the frontend dialogs
