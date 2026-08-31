# User Channels — API Reference

Auto-generated from OpenAPI spec. Tag: `user-channels`

## GET `/api/v1/users/me/channels`
**List My Channels**

---

## PUT `/api/v1/users/me/channels/{channel_id}`
**Update My Channel**

**Path parameters:**
- `channel_id`: uuid

**Request body** (`UserChannelUpdate`):
  - `is_enabled`: boolean | null
  - `agent_scope`: string | null
  - `agent_ids`: array | null
  - `pinned_agent_id`: string | null
  - `allow_identity_routing`: boolean | null

**Response:** `UserChannelPublic`

---

## DELETE `/api/v1/users/me/channels/{channel_id}`
**Reset My Channel**

**Path parameters:**
- `channel_id`: uuid

**Response:** `UserChannelPublic`

---
