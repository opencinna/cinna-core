# Improvement Requests — API Reference

Auto-generated from OpenAPI spec. Tag: `improvement-requests`

## GET `/api/v1/sessions/{session_id}/improvement-context`
**Get Improvement Context**

**Path parameters:**
- `session_id`: uuid

**Response:** `ImprovementContextPublic`

---

## POST `/api/v1/improvement-requests`
**Create Improvement Request**

**Request body** (`ImprovementRequestCreate`):
  - `session_id`: uuid (required)
  - `comment`: string | null
  - `include_memory`: boolean

**Response:** `ImprovementRequestPublic`

---

## GET `/api/v1/improvement-requests/mine`
**List My Improvement Requests**

**Query parameters:**
- `status`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `50`

**Response:** `ImprovementRequestsPublic`

---

## GET `/api/v1/agents/{agent_id}/improvement-requests`
**List Agent Improvement Requests**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `status`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `50`

**Response:** `ImprovementRequestsPublic`

---

## GET `/api/v1/improvement-requests/{request_id}`
**Get Improvement Request**

**Path parameters:**
- `request_id`: uuid

**Response:** `ImprovementRequestDetailPublic`

---

## PATCH `/api/v1/improvement-requests/{request_id}`
**Update Improvement Request**

**Path parameters:**
- `request_id`: uuid

**Request body** (`ImprovementRequestUpdate`):
  - `status`: string | null
  - `resolution_note`: string | null

**Response:** `ImprovementRequestDetailPublic`

---

## DELETE `/api/v1/improvement-requests/{request_id}`
**Delete Improvement Request**

**Path parameters:**
- `request_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/improvement-requests/{request_id}/archive`
**Download Improvement Archive**

**Path parameters:**
- `request_id`: uuid

---
