# Server Channels — API Reference

Auto-generated from OpenAPI spec. Tag: `server-channels`

## POST `/api/v1/channels/{webhook_token}/inbound`
**Channel Inbound**

**Path parameters:**
- `webhook_token`: string

---

## GET `/api/v1/admin/server-channels/channel-types`
**List Channel Types**

---

## GET `/api/v1/admin/server-channels/auto-install-list`
**List Auto Install Bundles**

---

## POST `/api/v1/admin/server-channels/auto-install-list`
**Add Auto Install Bundle**

**Request body** (`AutoInstallBundleAdd`):
  - `bundle_uuid`: uuid (required)

---

## DELETE `/api/v1/admin/server-channels/auto-install-list/{bundle_uuid}`
**Remove Auto Install Bundle**

**Path parameters:**
- `bundle_uuid`: uuid

---

## GET `/api/v1/admin/server-channels`
**List Channels**

---

## POST `/api/v1/admin/server-channels`
**Create Channel**

**Request body** (`ServerChannelCreate`):
  - `channel_type`: string (required)
  - `name`: string (required)
  - `enabled`: boolean
  - `auto_register_users`: boolean
  - `config`: object
  - `email_whitelist`: string | null
  - `secrets`: string | null

**Response:** `ServerChannelPublic`

---

## PUT `/api/v1/admin/server-channels/{channel_id}`
**Update Channel**

**Path parameters:**
- `channel_id`: uuid

**Request body** (`ServerChannelUpdate`):
  - `channel_type`: string | null
  - `name`: string | null
  - `enabled`: boolean | null
  - `auto_register_users`: boolean | null
  - `config`: object | null
  - `email_whitelist`: string | null
  - `secrets`: string | null
  - `regenerate_webhook_token`: boolean

**Response:** `ServerChannelPublic`

---

## DELETE `/api/v1/admin/server-channels/{channel_id}`
**Delete Channel**

**Path parameters:**
- `channel_id`: uuid

---

## GET `/api/v1/admin/server-channels/{channel_id}/setup-instructions`
**Get Setup Instructions**

**Path parameters:**
- `channel_id`: uuid

**Response:** `ChannelSetupInstructions`

---

## POST `/api/v1/admin/server-channels/{channel_id}/test-outbound`
**Test Outbound**

**Path parameters:**
- `channel_id`: uuid

**Request body** (`ChannelTestOutboundRequest`):
  - `email`: string | null
  - `thread_key`: string | null
  - `text`: string | null

**Response:** `ChannelTestOutboundResult`

---

## GET `/api/v1/admin/server-channels/{channel_id}/recent-senders`
**List Recent Senders**

**Path parameters:**
- `channel_id`: uuid

---

## GET `/api/v1/admin/server-channels/{channel_id}/debug-events`
**List Debug Events**

**Path parameters:**
- `channel_id`: uuid

**Response:** `ChannelDebugEventsPublic`

---

## DELETE `/api/v1/admin/server-channels/{channel_id}/debug-events`
**Clear Debug Events**

**Path parameters:**
- `channel_id`: uuid

**Response:** `Message`

---
