# Agent Shares — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-shares`

## POST `/api/v1/agents/{agent_id}/shares`
**Share Agent**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentShareCreate`):
  - `shared_with_email`: string (required)
  - `share_mode`: string (required)
  - `provide_ai_credentials`: boolean
  - `conversation_ai_credential_id`: string | null
  - `building_ai_credential_id`: string | null

**Response:** `AgentSharePublic`

---

## GET `/api/v1/agents/{agent_id}/shares`
**Get Agent Shares**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentSharesPublic`

---

## GET `/api/v1/agents/{agent_id}/clones`
**Get Agent Clones**

**Path parameters:**
- `agent_id`: uuid

---

## DELETE `/api/v1/agents/{agent_id}/shares/{share_id}`
**Revoke Share**

**Path parameters:**
- `agent_id`: uuid
- `share_id`: uuid

**Query parameters:**
- `action`: "delete" | "detach" | "remove" (required)

**Response:** `RevokeResponse`

---

## GET `/api/v1/shares/pending`
**Get Pending Shares**

**Response:** `PendingSharesPublic`

---

## POST `/api/v1/shares/{share_id}/accept`
**Accept Share**

**Path parameters:**
- `share_id`: uuid


**Response:** `AgentPublic`

---

## POST `/api/v1/shares/{share_id}/decline`
**Decline Share**

**Path parameters:**
- `share_id`: uuid

**Response:** `DeclineResponse`

---

## POST `/api/v1/agents/{agent_id}/detach`
**Detach Clone**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPublic`

---

## POST `/api/v1/agents/{agent_id}/shares/push-updates`
**Push Updates To Clones**

**Path parameters:**
- `agent_id`: uuid


**Response:** `PushUpdatesResponse`

---

## POST `/api/v1/agents/{agent_id}/apply-update`
**Apply Update**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPublic`

---

## GET `/api/v1/agents/{agent_id}/update-status`
**Get Update Status**

**Path parameters:**
- `agent_id`: uuid

**Response:** `UpdateStatusResponse`

---

## PATCH `/api/v1/agents/{agent_id}/update-mode`
**Set Update Mode**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`SetUpdateModeRequest`):
  - `update_mode`: string (required)

**Response:** `AgentPublic`

---

## GET `/api/v1/agents/{agent_id}/update-requests`
**Get Pending Update Requests**

**Path parameters:**
- `agent_id`: uuid

**Response:** `CloneUpdateRequestsPublic`

---

## POST `/api/v1/update-requests/{request_id}/dismiss`
**Dismiss Update Request**

**Path parameters:**
- `request_id`: uuid

**Response:** `CloneUpdateRequestPublic`

---
