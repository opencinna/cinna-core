# AgenticTeams

## Overview

AgenticTeams is a visual org-chart builder where users define named agentic teams, add agent nodes, and wire directed connections between nodes with handover prompts. The result is an interactive directed graph that represents agent orchestration topology — which agents participate, which is the entry point, and how work flows between them.

Teams are intentionally **workspace-independent**: a team can include agents from any workspace the user owns. Teams are owned directly by the user, not by a workspace, mirroring the same design decision made for Dashboards.

The current phase is the **Blueprint** — the chart is a static structural definition. It does not execute. Future phases will add live execution, human-in-the-loop nodes, and observability (see [product vision](../../drafts/agentic-teams-product-vision.md)).

---

## Glossary

| Term | Definition |
|------|-----------|
| **AgenticTeam** | Named collection of agent nodes and connections, owned by a user |
| **Node** | An agent participating in the team; carries name, position, and lead designation |
| **Connection** | Directed edge between two nodes; carries a handover prompt and enabled flag |
| **Team Lead** | The designated entry-point node for future process invocation; at most one per team |
| **Handover Prompt** | Text on a connection describing when and how the source agent passes work to the target agent |
| **Edit Mode** | Chart interaction mode where nodes can be dragged, added, and deleted, and connections can be drawn |
| **View Mode** | Read-only chart mode; connection edit/delete controls hidden |
| **Auto-Arrange** | One-click Dagre layout that reorders nodes into a top-down hierarchy with the lead node at top |

---

## User Flows

### Creating a Team

Two entry points exist:

**Via Sidebar Switcher**
1. User clicks the "Agentic Teams" item in the sidebar (shows "Agentic Teams" when no team is active, or the current team name when on a chart page).
2. Dropdown opens showing all existing teams and a "New Agentic Team" option at the bottom.
3. User clicks "New Agentic Team" — a create dialog appears.
4. User enters a name (required, 1–255 characters) and selects an icon (defaults to "users").
5. Team is created and the user is immediately navigated to `/agentic-teams/{teamId}`.

**Via Settings → Interface Tab**
1. User opens Settings, navigates to the Interface tab.
2. The "Agentic Teams" card (alongside Workspaces and Dashboards) shows existing teams and a "New Agentic Team" button.
3. User clicks "New Agentic Team" — same create dialog appears.
4. Team is created; user stays on the Settings page (no auto-navigate from settings).

### Navigating to a Team Chart

- Sidebar switcher dropdown lists all teams; clicking any team navigates to `/agentic-teams/{teamId}`.
- If teams exist, `/agentic-teams/` auto-redirects to the first team in the list.
- If no teams exist, `/agentic-teams/` shows the AgenticTeamSettings card with a prompt to create the first team.

### Editing Team Name or Icon

1. On the chart page, click the ⋮ (EllipsisVertical) menu in the page header.
2. Select "Edit Team" — a form dialog appears pre-filled with the current name and icon.
3. Update name or icon, click "Save".

### Deleting a Team

1. On the chart page, click ⋮ → "Delete Team".
2. A confirmation dialog warns that all nodes and connections will be removed.
3. On confirm, team is deleted and user is navigated to `/agentic-teams`.

Alternatively, from Settings → Interface → Agentic Teams card, click the trash icon next to any team.

---

### Adding Nodes

Nodes can only be added while in **edit mode**.

1. Click the lock/unlock toggle in the page header to enter edit mode. The chart background grid appears and node borders become dashed.
2. Click "Add Node" (top-right of the chart canvas area, only visible in edit mode).
3. The Add Node dialog opens, showing a dropdown of all agents owned by the user that are not already in this team.
4. User selects an agent from the dropdown. A preview line shows the node name that will be used (the agent's name).
5. Optionally toggle "Set as team lead".
6. Click "Add Node".

Business rules enforced at creation:
- The agent must be owned by the current user (403 if not).
- The same agent cannot appear twice in the same team (409 Conflict returned).
- If "Set as team lead" is enabled and another node is already lead, the previous lead is automatically unmarked.
- Node name is auto-populated from the agent's `name` field; it cannot be set by the user directly.
- Node color is inherited from the agent's `ui_color_preset`; it cannot be set independently.

### Setting the Team Lead

From the chart in edit mode:
1. Hover over a node — a ⋮ (kebab) menu appears.
2. Click "Set as Lead" (or "Unset Lead" if already lead).
3. A crown badge appears on the lead node.

At most one lead node per team is enforced: marking a new node as lead automatically unmarks the previous lead node in the same API call.

### Removing Nodes

In edit mode, open the node's kebab menu and click "Remove Node". The node and all its incoming and outgoing connections are deleted (CASCADE behavior).

### Drawing Connections

Connections can only be created in **edit mode**.

1. In edit mode, source and target handles (small circles) appear at the top and bottom of each node.
2. Drag from the bottom handle of the source node to the top handle of the target node.
3. The connection is created immediately with an empty handover prompt and `enabled = true`.

Business rules enforced at creation:
- Source and target must be different nodes (self-connections rejected with 400).
- Both nodes must belong to the same team.
- Only one connection per `(source, target)` pair is allowed (409 Conflict if a duplicate is attempted).

### Editing Connections

Connection editing is available in **edit mode only**, via a hover control on the edge.

1. In edit mode, hovering near the midpoint of any connection reveals a small pencil icon button.
2. Clicking the pencil opens a popover with "Edit" and "Delete" options.
3. "Edit" opens the Connection Edit Dialog showing:
   - Source and target agent names as **colored badges** (Bot icon + agent name, styled with each agent's color preset), separated by an arrow icon
   - Handover Prompt textarea (max 2000 characters) with a character counter
   - A **"Generate" button** (Sparkles icon) next to the "Handover Prompt" label that calls an AI function to auto-generate a handover prompt based on both agents' configurations
   - "Connection enabled" toggle switch
4. User edits the prompt (or clicks Generate to have AI draft it) and clicks "Save".

**AI Prompt Generation:**
- Clicking "Generate" calls the backend endpoint `POST /{team_id}/connections/{conn_id}/generate-prompt`.
- The button shows "Generating..." and is disabled while the request is in flight.
- When the response arrives, the generated text is automatically placed into the textarea (replacing whatever was there), ready for the user to review or edit before saving.
- Generation uses the same AI function used by agent handover prompt generation (`AIFunctionsService.generate_handover_prompt`), passing both agents' names, entrypoint prompts, and workflow prompts as context.
- If generation fails, an error toast is shown and the textarea is not modified.

Enabled/disabled state is reflected visually: disabled connections render with a dashed stroke.

### Deleting Connections

In edit mode, hover the connection midpoint → click the pencil popover → "Delete", or use the trash icon in the Connection Edit Dialog popover.

### Auto-Arrange

The "Auto-Arrange" button is always visible in the top-right of the chart canvas (in both view and edit modes).

Clicking it runs a Dagre top-down (`rankdir: TB`) layout algorithm client-side:
- All nodes are repositioned into a hierarchical structure.
- Node separation: 80px horizontal, 120px vertical rank spacing.
- Lead node appears at top if connections flow from it.

After arranging, all new positions are persisted to the backend in a single bulk positions call.

---

## Business Rules

### Access Control

- **Owner-only access**: only the user who owns a team can read or modify it.
- Superuser bypass does **not** apply — even superusers cannot access other users' teams.
- All node and connection endpoints verify team ownership before performing any operation.
- A 404 (not 403) is returned for unauthorized access to prevent information leakage about other users' teams.

### Agent Uniqueness Per Team

An agent can only appear once in any given team. Attempting to add the same agent twice returns 409 Conflict.

### Lead Node Constraint

At most one node per team can have `is_lead = true`. Setting a new lead automatically unmarks the previous lead — no explicit "unset" step is needed. A team with zero lead nodes is valid.

### Node Name Immutability

Once created, a node's name and agent association cannot be changed. To update either, delete the node and re-add it with the correct agent.

### Cascade Deletes

| Deleted entity | What is also deleted |
|----------------|---------------------|
| Team | All nodes and connections for that team |
| Node | All connections where the node is source or target |
| Agent (platform-wide) | All nodes linked to that agent (across all teams) |
| User | All teams owned by that user (and transitively all nodes and connections) |

### Position Persistence

Node positions (`pos_x`, `pos_y`) are stored in the database. After a drag-and-drop in edit mode, positions are saved via a debounced (300ms) bulk update call. Auto-arrange also persists immediately via the bulk positions endpoint.

Position updates do not invalidate the chart query cache — they are treated as pure UI state that is already reflected locally.

---

## Integration Points

### Agents

- Each node requires an agent (`agent_id` is mandatory, not nullable).
- Node name is sourced from `agent.name` at creation time.
- Node color is sourced from `agent.ui_color_preset` at read time (resolved in the service layer, not stored on the node).
- Deleting an agent cascades to remove all its nodes across all teams.
- The Add Node Dialog filters out agents already in the team, showing only available agents.

### Task-Based Collaboration

Teams participate directly in the task management system:

- **Task prefix**: Each team has an optional `task_prefix` field (e.g., `"HR"`). When set, tasks created in the team's context use this string as the short-code prefix (`HR-1`, `HR-42`). When null, the default `"TASK"` prefix is used. The prefix is configurable in team settings (1–10 uppercase alphanumeric characters).
- **Team-scoped tasks**: Tasks with `team_id` set are associated with a team. The task board can filter by `team_id` to show only a team's work. Team membership unlocks the `mcp__agent_task__create_subtask` tool.
- **Subtask delegation topology**: When an agent calls `mcp__agent_task__create_subtask`, the backend enforces that a directed connection exists from the creating agent's node to the target node in the team graph. Disconnected or unknown target agents are rejected. This means the visual team chart is the authoritative delegation policy — draw a connection to permit handover.
- **Connection prompts**: The `handover_prompt` on a connection is injected into the target agent's task context prompt explaining why they received the subtask and what context the parent agent provides.

See [Input Tasks](../../application/input_tasks/input_tasks.md) for the full task collaboration model.

### Workspaces

AgenticTeams are explicitly workspace-independent. Teams are not filtered by workspace and do not carry a `user_workspace_id`. This mirrors the same design decision as Dashboards.

### User Settings (Interface Tab)

The AgenticTeamSettings card is embedded in the Interface settings tab alongside the Workspaces and Dashboards cards. It provides the same create/edit/delete capabilities as the chart-page header controls. Team task prefix is editable from this card.

### Sidebar Navigation

The `AgenticTeamsSwitcher` component renders as a `SidebarMenuItem` above the workspace switcher. It mirrors the workspace switcher pattern (dropdown with items + "New ..." action).

---

## View Mode vs Edit Mode

| Capability | View Mode | Edit Mode |
|-----------|-----------|-----------|
| Pan and zoom chart | Yes | Yes |
| Auto-Arrange | Yes | Yes |
| See connection prompts | No (view dialog not implemented in MVP) | Yes (via hover menu) |
| Drag nodes | No | Yes |
| Add nodes | No | Yes |
| Draw connections | No | Yes |
| Edit/delete connections | No | Yes (via midpoint hover) |
| Delete nodes | No | Yes (via kebab menu) |
| Set team lead | No | Yes (via kebab menu) |
| Background grid | No | Yes |
| Node handles visible | No | Yes |
| Node borders | Solid | Dashed |

Mode is local client state (`isEditMode: boolean`) — not persisted. The page always opens in view mode.

---

## Empty States

- **No teams**: `/agentic-teams/` shows the AgenticTeamSettings card with an explanatory message.
- **No nodes on a chart**: A centered message reads "No nodes yet. Switch to edit mode to add team members."
- **All agents already added**: Add Node Dialog shows "All your agents are already in this team" and the Add button is disabled.

---

## Future Phases

The product vision defines 9 additional phases beyond the current Blueprint:

- **Phase 2**: Execution Engine — teams that actually run; execution sessions, real-time trace overlay, parallel/sequential execution
- **Phase 3**: Human-in-the-Loop — human nodes with channel configuration (email, Slack, WhatsApp), async execution, availability scheduling
- **Phase 4**: Connection Intelligence — conditional routing, pre-handover transforms, feedback loops (backward edges), SLA tracking
- **Phase 5**: Shared Context and Knowledge — team scratchpad, shared knowledge base, artifact passing, cross-session memory
- **Phase 6**: Templates and Marketplace — curated industry templates, onboarding wizard, community marketplace
- **Phase 7**: Observability and Analytics — per-team dashboards, cost tracking, bottleneck identification, alerting
- **Phase 8**: Multi-Tenancy — organization layer, role-based access, team-of-teams composition
- **Phase 9**: Internationalization — multilingual handovers, timezone-aware execution, RTL support
- **Phase 10**: Safety and Governance — guardrails, budget controls, immutable audit trail, human override

See [product vision](../../drafts/agentic-teams-product-vision.md) for full detail on each phase.
