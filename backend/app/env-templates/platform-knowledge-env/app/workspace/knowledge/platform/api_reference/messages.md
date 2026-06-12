# Messages — API Reference

Auto-generated from OpenAPI spec. Tag: `messages`

## GET `/api/v1/sessions/{session_id}/messages`
**Get Messages**

**Path parameters:**
- `session_id`: uuid

**Query parameters:**
- `limit`: integer, default: `100`
- `offset`: integer, default: `0`

**Response:** `MessagesPublic`

---

## POST `/api/v1/sessions/{session_id}/messages/stream`
**Send Message Stream**

**Path parameters:**
- `session_id`: uuid

**Request body** (`MessageCreate`):
  - `content`: string (required)
  - `answers_to_message_id`: string | null
  - `file_ids`: uuid[]
  - `page_context`: string | null

---

## POST `/api/v1/sessions/{session_id}/messages/interrupt`
**Interrupt Message**

**Path parameters:**
- `session_id`: uuid

---

## GET `/api/v1/sessions/{session_id}/messages/streaming-status`
**Get Streaming Status**

**Path parameters:**
- `session_id`: uuid

---

## GET `/api/v1/sessions/{session_id}/commands`
**List Session Commands**

**Path parameters:**
- `session_id`: uuid

**Response:** `SessionCommandsPublic`

---
