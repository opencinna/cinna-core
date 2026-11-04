# Dashboardchat — API Reference

Auto-generated from OpenAPI spec. Tag: `DashboardChat`

## POST `/api/v1/dashboards/component-refs/resolve`
**Resolve Component Refs**

**Request body** (`ComponentRefsResolveRequest`):
  - `refs`: string[] (required)
  - `session_id`: string | null

**Response:** `ComponentRefsResolved`

---

## GET `/api/v1/dashboards/{dashboard_id}/chat`
**Get Dashboard Chat**

**Path parameters:**
- `dashboard_id`: uuid

**Query parameters:**
- `agent_id`: uuid (required)

**Response:** `DashboardChatPublic`

---

## POST `/api/v1/dashboards/{dashboard_id}/chat`
**Open Dashboard Chat**

**Path parameters:**
- `dashboard_id`: uuid

**Request body** (`DashboardChatCreate`):
  - `agent_id`: uuid (required)
  - `new`: boolean

**Response:** `DashboardChatPublic`

---
