# AgenticTeams

## Overview

AgenticTeams is a visual org-chart builder where users define named agentic teams, add agent nodes, and wire directed connections between nodes with handover prompts. Each team produces an interactive chart page with view/edit mode toggle, manual node positioning, and an auto-arrange button.

The feature is named "AgenticTeams" (rather than just "Teams") to distinguish it from future user/permission team management. AgenticTeams focus on **agent orchestration topology** — defining which agents play which roles and how they hand off work to each other.

---

## Core Concepts

### AgenticTeam

A top-level named container (name + icon), workspace-independent. Teams are personal — only the owner can view or manage them. A user can have unlimited teams.

### AgenticTeamNode

A node in the team org-chart. Currently all nodes represent agents (`node_type: "agent"`). Key properties:

- **name**: Auto-populated from the agent's name on creation. Not updatable — to rename a node, delete and re-add the agent.
- **agent_id**: Required. Same agent cannot appear twice in the same team.
- **is_lead**: Boolean marking one node as the team entry point. At most one per team. Setting a node as lead automatically unmarks the previous lead.
- **pos_x / pos_y**: 2D position for chart rendering.
- **agent_ui_color_preset**: Resolved at query time from the linked agent — not stored on the node.

When an agent is deleted, all its nodes across all teams are cascade-deleted (FK cascade).

### AgenticTeamConnection

A directed edge between two nodes. Properties:

- **connection_prompt**: Handover instruction following the 2-3 sentence convention (trigger conditions, context to pass, expected format). Max 2000 characters.
- **enabled**: When false, the edge renders as dashed in the chart.
- **source_node_name / target_node_name**: Resolved at query time from the linked nodes.

Business constraints enforced at service layer:
- No self-connections (`source_node_id != target_node_id`)
- No duplicate `(source_node_id, target_node_id)` pair per team

Cascade: deleting a node removes all its incoming and outgoing connections.

---

## User Flows

### Creating a Team

1. Settings → Interface tab → "Agentic Teams" card → "New Agentic Team"
2. Enter name, pick icon, submit
3. Team appears in the settings list and the sidebar Agentic Teams switcher

### Opening a Team Chart

1. Click team name in the sidebar Agentic Teams switcher → navigates to `/agentic-teams/{id}`
2. Chart page loads via `GET /api/v1/agentic-teams/{id}/chart` (single round-trip)
3. Nodes and connections rendered in view mode (read-only)

### Adding a Node

1. Click "Edit Layout" in page header to enter edit mode
2. Click "+ Add Node" button
3. Select an agent from the dropdown (agents already in the team are excluded)
4. Optionally enable "Set as team lead"
5. Node appears at a default position; drag to desired location
6. Positions auto-save after 300ms debounce via bulk positions endpoint

### Setting the Team Lead

In edit mode, use the node's kebab menu → "Set as Lead". The lead node:
- Displays a crown badge in the top-right corner
- Gets a gold ring border
- Is placed at the top during Auto-Arrange

### Adding a Connection

1. In edit mode, drag from a node's bottom handle to another node
2. Connection created with empty prompt
3. Hover the connection line → click the midpoint button → "Edit" to add the handover prompt

### Auto-Arrange

Click "Auto-Arrange" (top-right of the chart area). The Dagre layout algorithm computes a top-down hierarchy, placing the lead node at the top. Positions are saved via the bulk positions endpoint.

---

## Access Control

- All endpoints require JWT authentication.
- Owner-only access — **no superuser bypass**. Only the team owner can read or modify their teams, nodes, and connections.
- Both not-found and wrong-owner return 404 (security through obscurity — never reveal whether a team exists to other users).

---

## Sidebar Integration

`AgenticTeamsSwitcher` appears in the sidebar footer above `SidebarWorkspaceSwitcher`. It shows the active team name (determined from the current route), or "Agentic Teams" when not on a team chart page.

---

## Settings Integration

`AgenticTeamSettings` card appears in the Interface tab of User Settings alongside the Workspaces card. Both cards are rendered in a vertical stack within the same tab.

---

## Workspace Independence

AgenticTeams have no `user_workspace_id` column. Teams are visible across all workspaces — the same as dashboards. This is intentional: agent orchestration topology is workspace-agnostic.

---

## Integration Points

| System | How It Connects |
|--------|----------------|
| Agent model | `agent_id` FK on nodes; `ui_color_preset` and `name` resolved at query time |
| Agent deletion | FK CASCADE removes nodes from all teams automatically |
| Workspaces | Not connected — teams are workspace-independent |
| Dashboards | Same view/edit mode pattern; same sidebar footer placement pattern |
| AgentHandovers | `connection_prompt` mirrors `handover_prompt` — same 2-3 sentence convention |

---

## Future Enhancements (Post-MVP)

- **Team execution engine**: Invoke a team on a task; lead node receives, delegates via connections
- **Human-in-the-loop nodes**: `node_type: "human"` with email/Slack channels
- **Conditional routing**: Connection conditions ("if output contains X, go to node A")
- **AI-generated connection prompts**: "Generate" button like AgentHandovers
- **Execution trace**: Real-time visualization of active nodes and message flow
- **Team templates**: Pre-built structures (IT Support, Content Pipeline, etc.)
