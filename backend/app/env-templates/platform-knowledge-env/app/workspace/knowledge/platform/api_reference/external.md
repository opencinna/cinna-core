# External — API Reference

Auto-generated from OpenAPI spec. Tag: `external`

## GET `/api/v1/external/agents`
**List External Agents**

**Query parameters:**
- `workspace_id`: string | null

**Response:** `ExternalAgentListResponse`

---

## GET `/api/v1/external/sessions`
**List External Sessions**

**Query parameters:**
- `limit`: integer, default: `100`
- `offset`: integer, default: `0`

---

## GET `/api/v1/external/sessions/{session_id}`
**Get External Session**

**Path parameters:**
- `session_id`: uuid

**Response:** `ExternalSessionPublic`

---

## DELETE `/api/v1/external/sessions/{session_id}`
**Hide External Session**

**Path parameters:**
- `session_id`: uuid

---

## GET `/api/v1/external/sessions/{session_id}/messages`
**List External Session Messages**

**Path parameters:**
- `session_id`: uuid

---
