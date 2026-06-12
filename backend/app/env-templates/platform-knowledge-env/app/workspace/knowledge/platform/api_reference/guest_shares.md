# Guest Shares — API Reference

Auto-generated from OpenAPI spec. Tag: `guest-shares`

## POST `/api/v1/agents/{agent_id}/guest-shares/`
**Create Guest Share**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentGuestShareCreate`):
  - `label`: string | null
  - `expires_in_hours`: integer
  - `allow_env_panel`: boolean

**Response:** `AgentGuestShareCreated`

---

## GET `/api/v1/agents/{agent_id}/guest-shares/`
**List Guest Shares**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentGuestSharesPublic`

---

## GET `/api/v1/agents/{agent_id}/guest-shares/{guest_share_id}`
**Get Guest Share**

**Path parameters:**
- `agent_id`: uuid
- `guest_share_id`: uuid

**Response:** `AgentGuestSharePublic`

---

## DELETE `/api/v1/agents/{agent_id}/guest-shares/{guest_share_id}`
**Delete Guest Share**

**Path parameters:**
- `agent_id`: uuid
- `guest_share_id`: uuid

**Response:** `Message`

---

## PUT `/api/v1/agents/{agent_id}/guest-shares/{guest_share_id}`
**Update Guest Share**

**Path parameters:**
- `agent_id`: uuid
- `guest_share_id`: uuid

**Request body** (`AgentGuestShareUpdate`):
  - `label`: string | null
  - `security_code`: string | null
  - `allow_env_panel`: boolean | null

**Response:** `AgentGuestSharePublic`

---
