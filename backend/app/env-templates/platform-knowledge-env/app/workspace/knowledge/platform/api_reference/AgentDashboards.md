# Agentdashboards — API Reference

Auto-generated from OpenAPI spec. Tag: `AgentDashboards`

## GET `/api/v1/agents/{agent_id}/dashboards/`
**List Agent Dashboard Components**

**Path parameters:**
- `agent_id`: uuid

**Response:** `DashboardComponentsPublic`

---

## GET `/api/v1/agents/{agent_id}/dashboards/{name}`
**Get Agent Dashboard Component**

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Query parameters:**
- `skill`: string | null

**Response:** `DashboardComponentPublic`

---

## POST `/api/v1/agents/{agent_id}/dashboards/{name}/run`
**Run Agent Dashboard Component**

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Request body** (`DashboardComponentRunRequest`):
  - `skill`: string | null
  - `endpoint`: string | null
  - `params`: object
  - `tile_key`: string | null

**Response:** `DashboardComponentRunResult`

---

## POST `/api/v1/agents/{agent_id}/dashboards/{name}/frame`
**Mint Agent Dashboard Component Frame**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Request body** (`DashboardComponentFrameRequest`):
  - `skill`: string | null
  - `tile_key`: string | null

**Response:** `DashboardFramePublic`

---
