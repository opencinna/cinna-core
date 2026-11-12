# Agent Api Requests — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-api-requests`

## POST `/api/v1/agent/api-requests/list`
**Agent Api Requests List**

**Request body** (`AgentApiRequestToolListRequest`):
  - `status`: string | null
  - `limit`: integer

---

## POST `/api/v1/agent/api-requests/get`
**Agent Api Requests Get**

**Request body** (`AgentApiRequestToolGetRequest`):
  - `request_id`: uuid (required)

---

## POST `/api/v1/agent/api-requests/update-status`
**Agent Api Requests Update Status**

**Request body** (`AgentApiRequestToolUpdateStatusRequest`):
  - `request_id`: uuid (required)
  - `status`: string (required)
  - `resolution_note`: string | null

---
