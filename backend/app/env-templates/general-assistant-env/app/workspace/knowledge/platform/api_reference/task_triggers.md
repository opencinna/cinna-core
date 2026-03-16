# Task Triggers — API Reference

Auto-generated from OpenAPI spec. Tag: `task-triggers`

## POST `/api/v1/tasks/{task_id}/triggers/schedule`
**Create Schedule Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateSchedule`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `natural_language`: string (required)
  - `timezone`: string (required)

**Response:** `TaskTriggerPublic`

---

## POST `/api/v1/tasks/{task_id}/triggers/schedule`
**Create Schedule Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateSchedule`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `natural_language`: string (required)
  - `timezone`: string (required)

**Response:** `TaskTriggerPublic`

---

## POST `/api/v1/tasks/{task_id}/triggers/exact-date`
**Create Exact Date Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateExactDate`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `execute_at`: datetime (required)
  - `timezone`: string (required)

**Response:** `TaskTriggerPublic`

---

## POST `/api/v1/tasks/{task_id}/triggers/exact-date`
**Create Exact Date Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateExactDate`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `execute_at`: datetime (required)
  - `timezone`: string (required)

**Response:** `TaskTriggerPublic`

---

## POST `/api/v1/tasks/{task_id}/triggers/webhook`
**Create Webhook Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateWebhook`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null

**Response:** `TaskTriggerPublicWithToken`

---

## POST `/api/v1/tasks/{task_id}/triggers/webhook`
**Create Webhook Trigger**

**Path parameters:**
- `task_id`: uuid

**Request body** (`TaskTriggerCreateWebhook`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null

**Response:** `TaskTriggerPublicWithToken`

---

## GET `/api/v1/tasks/{task_id}/triggers`
**List Triggers**

**Path parameters:**
- `task_id`: uuid

**Response:** `TaskTriggersPublic`

---

## GET `/api/v1/tasks/{task_id}/triggers`
**List Triggers**

**Path parameters:**
- `task_id`: uuid

**Response:** `TaskTriggersPublic`

---

## GET `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Get Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `TaskTriggerPublic`

---

## GET `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Get Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `TaskTriggerPublic`

---

## PATCH `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Update Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Request body** (`TaskTriggerUpdate`):
  - `name`: string | null
  - `enabled`: boolean | null
  - `payload_template`: string | null
  - `natural_language`: string | null
  - `timezone`: string | null
  - `execute_at`: string | null

**Response:** `TaskTriggerPublic`

---

## PATCH `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Update Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Request body** (`TaskTriggerUpdate`):
  - `name`: string | null
  - `enabled`: boolean | null
  - `payload_template`: string | null
  - `natural_language`: string | null
  - `timezone`: string | null
  - `execute_at`: string | null

**Response:** `TaskTriggerPublic`

---

## DELETE `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Delete Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `object`

---

## DELETE `/api/v1/tasks/{task_id}/triggers/{trigger_id}`
**Delete Trigger**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `object`

---

## POST `/api/v1/tasks/{task_id}/triggers/{trigger_id}/regenerate-token`
**Regenerate Token**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `TaskTriggerPublicWithToken`

---

## POST `/api/v1/tasks/{task_id}/triggers/{trigger_id}/regenerate-token`
**Regenerate Token**

**Path parameters:**
- `task_id`: uuid
- `trigger_id`: uuid

**Response:** `TaskTriggerPublicWithToken`

---
