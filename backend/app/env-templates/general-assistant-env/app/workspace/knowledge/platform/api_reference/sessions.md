# Sessions — API Reference

Auto-generated from OpenAPI spec. Tag: `sessions`

## POST `/api/v1/sessions/`
**Create Session**

**Request body** (`SessionCreate`):
  - `agent_id`: uuid (required)
  - `title`: string | null
  - `mode`: string
  - `guest_share_id`: string | null
  - `webapp_share_id`: string | null
  - `dashboard_block_id`: string | null

**Response:** `SessionPublic`

---

## GET `/api/v1/sessions/`
**List Sessions**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `order_by`: string, default: `created_at`
- `order_desc`: boolean, default: `True`
- `user_workspace_id`: string | null
- `agent_id`: string | null
- `guest_share_id`: string | null

**Response:** `SessionsPublicExtended`

---

## GET `/api/v1/sessions/{id}`
**Get Session**

**Path parameters:**
- `id`: uuid

**Response:** `SessionPublicExtended`

---

## PATCH `/api/v1/sessions/{id}`
**Update Session**

**Path parameters:**
- `id`: uuid

**Request body** (`SessionUpdate`):
  - `title`: string | null
  - `status`: string | null
  - `interaction_status`: string | null
  - `mode`: string | null

**Response:** `SessionPublic`

---

## DELETE `/api/v1/sessions/{id}`
**Delete Session**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## PATCH `/api/v1/sessions/{id}/mode`
**Switch Session Mode**

**Path parameters:**
- `id`: uuid

**Query parameters:**
- `new_mode`: string (required)
- `clear_external_session`: boolean, default: `False`

**Response:** `SessionPublicExtended`

---

## POST `/api/v1/sessions/{id}/reset-sdk`
**Reset Sdk Session**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/sessions/{id}/recover`
**Recover Session**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/sessions/bulk-delete`
**Bulk Delete Sessions**

**Request body** (`BulkDeleteRequest`):
  - `session_ids`: uuid[] (required)

**Response:** `BulkDeleteResponse`

---
