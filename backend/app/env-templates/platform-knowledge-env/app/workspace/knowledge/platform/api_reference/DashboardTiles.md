# Dashboardtiles — API Reference

Auto-generated from OpenAPI spec. Tag: `DashboardTiles`

## GET `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/data`
**Get Dashboard Tile Data**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Response:** `DashboardTileDataPublic`

---

## POST `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/refresh`
**Refresh Dashboard Tile**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`DashboardTileRefreshRequest`):
  - `reason`: "open" | "reshow" | "manual" | "interval" | "after_turn" | "params" (required)

**Response:** `DashboardTileDataPublic`

---

## PUT `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/params`
**Set Dashboard Tile Params**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`DashboardTileParamsRequest`):
  - `params`: object

**Response:** `DashboardTileDataPublic`

---

## POST `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/call`
**Call Dashboard Tile Endpoint**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`DashboardTileCallRequest`):
  - `endpoint`: string (required)
  - `params`: object

**Response:** `DashboardTileCallResult`

---

## POST `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/frame`
**Mint Dashboard Tile Frame**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Response:** `DashboardFramePublic`

---

## PUT `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/component/page-state`
**Set Dashboard Tile Page State**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`DashboardTilePageStateRequest`):
  - `page_doc`: string | null
  - `highlight_ready`: boolean
  - `seq`: integer | null
  - `if_page_doc`: string | null

---
