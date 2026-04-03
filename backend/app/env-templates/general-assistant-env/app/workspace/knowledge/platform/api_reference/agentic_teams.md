# Agentic Teams — API Reference

Auto-generated from OpenAPI spec. Tag: `agentic-teams`

## GET `/api/v1/agentic-teams/`
**List Agentic Teams**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `AgenticTeamsPublic`

---

## POST `/api/v1/agentic-teams/`
**Create Agentic Team**

**Request body** (`AgenticTeamCreate`):
  - `name`: string (required)
  - `icon`: string | null
  - `task_prefix`: string | null

**Response:** `AgenticTeamPublic`

---

## GET `/api/v1/agentic-teams/{team_id}`
**Get Agentic Team**

**Path parameters:**
- `team_id`: uuid

**Response:** `AgenticTeamPublic`

---

## PUT `/api/v1/agentic-teams/{team_id}`
**Update Agentic Team**

**Path parameters:**
- `team_id`: uuid

**Request body** (`AgenticTeamUpdate`):
  - `name`: string | null
  - `icon`: string | null
  - `task_prefix`: string | null

**Response:** `AgenticTeamPublic`

---

## DELETE `/api/v1/agentic-teams/{team_id}`
**Delete Agentic Team**

**Path parameters:**
- `team_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agentic-teams/{team_id}/chart`
**Get Agentic Team Chart**

**Path parameters:**
- `team_id`: uuid

**Response:** `AgenticTeamChartPublic`

---

## PUT `/api/v1/agentic-teams/{team_id}/nodes/positions`
**Bulk Update Node Positions**

**Path parameters:**
- `team_id`: uuid


---

## GET `/api/v1/agentic-teams/{team_id}/nodes/`
**List Team Nodes**

**Path parameters:**
- `team_id`: uuid

**Response:** `AgenticTeamNodesPublic`

---

## POST `/api/v1/agentic-teams/{team_id}/nodes/`
**Create Team Node**

**Path parameters:**
- `team_id`: uuid

**Request body** (`AgenticTeamNodeCreate`):
  - `agent_id`: uuid (required)
  - `is_lead`: boolean
  - `pos_x`: number
  - `pos_y`: number

**Response:** `AgenticTeamNodePublic`

---

## GET `/api/v1/agentic-teams/{team_id}/nodes/{node_id}`
**Get Team Node**

**Path parameters:**
- `team_id`: uuid
- `node_id`: uuid

**Response:** `AgenticTeamNodePublic`

---

## PUT `/api/v1/agentic-teams/{team_id}/nodes/{node_id}`
**Update Team Node**

**Path parameters:**
- `team_id`: uuid
- `node_id`: uuid

**Request body** (`AgenticTeamNodeUpdate`):
  - `is_lead`: boolean | null
  - `pos_x`: number | null
  - `pos_y`: number | null

**Response:** `AgenticTeamNodePublic`

---

## DELETE `/api/v1/agentic-teams/{team_id}/nodes/{node_id}`
**Delete Team Node**

**Path parameters:**
- `team_id`: uuid
- `node_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agentic-teams/{team_id}/connections/`
**List Team Connections**

**Path parameters:**
- `team_id`: uuid

**Response:** `AgenticTeamConnectionsPublic`

---

## POST `/api/v1/agentic-teams/{team_id}/connections/`
**Create Team Connection**

**Path parameters:**
- `team_id`: uuid

**Request body** (`AgenticTeamConnectionCreate`):
  - `source_node_id`: uuid (required)
  - `target_node_id`: uuid (required)
  - `connection_prompt`: string
  - `enabled`: boolean

**Response:** `AgenticTeamConnectionPublic`

---

## GET `/api/v1/agentic-teams/{team_id}/connections/{conn_id}`
**Get Team Connection**

**Path parameters:**
- `team_id`: uuid
- `conn_id`: uuid

**Response:** `AgenticTeamConnectionPublic`

---

## PUT `/api/v1/agentic-teams/{team_id}/connections/{conn_id}`
**Update Team Connection**

**Path parameters:**
- `team_id`: uuid
- `conn_id`: uuid

**Request body** (`AgenticTeamConnectionUpdate`):
  - `connection_prompt`: string | null
  - `enabled`: boolean | null

**Response:** `AgenticTeamConnectionPublic`

---

## DELETE `/api/v1/agentic-teams/{team_id}/connections/{conn_id}`
**Delete Team Connection**

**Path parameters:**
- `team_id`: uuid
- `conn_id`: uuid

**Response:** `Message`

---
