# Events — API Reference

Auto-generated from OpenAPI spec. Tag: `events`

## POST `/api/v1/events/broadcast`
**Broadcast Event**

**Request body** (`EventBroadcast`):
  - `type`: string (required) — Event type
  - `model_id`: string | null — ID of the related model
  - `text_content`: string | null — Optional notification text
  - `meta`: object | null — Additional metadata
  - `user_id`: string | null — Target user ID (None for broadcast)
  - `room`: string | null — Room name for targeted broadcast (e.g., 'user_{user_id}')

---

## GET `/api/v1/events/stats`
**Get Connection Stats**

---

## POST `/api/v1/events/test`
**Test Event**

---
