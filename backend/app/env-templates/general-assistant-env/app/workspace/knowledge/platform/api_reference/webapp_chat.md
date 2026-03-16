# Webapp Chat — API Reference

Auto-generated from OpenAPI spec. Tag: `webapp-chat`

## POST `/api/v1/webapp/{token}/chat/sessions`
**Create Or Get Chat Session**

**Path parameters:**
- `token`: string

**Response:** `SessionPublic`

---

## GET `/api/v1/webapp/{token}/chat/sessions`
**Get Active Chat Session**

**Path parameters:**
- `token`: string

---

## GET `/api/v1/webapp/{token}/chat/sessions/{session_id}`
**Get Chat Session**

**Path parameters:**
- `token`: string
- `session_id`: uuid

**Response:** `SessionPublic`

---

## GET `/api/v1/webapp/{token}/chat/sessions/{session_id}/messages`
**Get Chat Messages**

**Path parameters:**
- `token`: string
- `session_id`: uuid

**Query parameters:**
- `limit`: integer, default: `100`
- `offset`: integer, default: `0`

**Response:** `MessagesPublic`

---

## POST `/api/v1/webapp/{token}/chat/sessions/{session_id}/messages/stream`
**Send Chat Message Stream**

**Path parameters:**
- `token`: string
- `session_id`: uuid

**Request body** (`MessageCreate`):
  - `content`: string (required)
  - `answers_to_message_id`: string | null
  - `file_ids`: uuid[]
  - `page_context`: string | null

---

## POST `/api/v1/webapp/{token}/chat/sessions/{session_id}/messages/interrupt`
**Interrupt Chat Message**

**Path parameters:**
- `token`: string
- `session_id`: uuid

---
