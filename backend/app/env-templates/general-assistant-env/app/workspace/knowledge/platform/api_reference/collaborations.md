# Collaborations — API Reference

Auto-generated from OpenAPI spec. Tag: `collaborations`

## POST `/api/v1/agents/collaborations/create`
**Create Collaboration**

**Request body** (`CreateCollaborationRequest`):
  - `title`: string (required)
  - `description`: string | null
  - `subtasks`: object[] (required)
  - `source_session_id`: string (required)

**Response:** `CreateCollaborationResponse`

---

## POST `/api/v1/agents/collaborations/{collaboration_id}/findings`
**Post Finding**

**Path parameters:**
- `collaboration_id`: uuid

**Request body** (`PostFindingRequest`):
  - `finding`: string (required)
  - `source_session_id`: string | null

**Response:** `PostFindingResponse`

---

## GET `/api/v1/agents/collaborations/{collaboration_id}/status`
**Get Collaboration Status**

**Path parameters:**
- `collaboration_id`: uuid

**Response:** `AgentCollaborationPublic`

---

## GET `/api/v1/agents/collaborations/by-session/{session_id}`
**Get Collaboration By Session**

**Path parameters:**
- `session_id`: uuid

---
