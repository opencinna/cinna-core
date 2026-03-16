# Activities — API Reference

Auto-generated from OpenAPI spec. Tag: `activities`

## POST `/api/v1/activities/`
**Create Activity**

**Request body** (`ActivityCreate`):
  - `session_id`: string | null
  - `agent_id`: string | null
  - `input_task_id`: string | null
  - `activity_type`: string (required)
  - `text`: string (required)
  - `action_required`: string
  - `is_read`: boolean

**Response:** `ActivityPublic`

---

## GET `/api/v1/activities/`
**List Activities**

**Query parameters:**
- `agent_id`: string | null
- `user_workspace_id`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `order_desc`: boolean, default: `True`

**Response:** `ActivitiesPublicExtended`

---

## DELETE `/api/v1/activities/`
**Delete All Activities**

---

## GET `/api/v1/activities/stats`
**Get Activity Stats**

**Response:** `ActivityStats`

---

## PATCH `/api/v1/activities/{activity_id}`
**Update Activity**

**Path parameters:**
- `activity_id`: uuid

**Request body** (`ActivityUpdate`):
  - `is_read`: boolean | null

**Response:** `ActivityPublic`

---

## DELETE `/api/v1/activities/{activity_id}`
**Delete Activity**

**Path parameters:**
- `activity_id`: uuid

---

## POST `/api/v1/activities/mark-read`
**Mark Activities As Read**


**Response:** `object`

---
