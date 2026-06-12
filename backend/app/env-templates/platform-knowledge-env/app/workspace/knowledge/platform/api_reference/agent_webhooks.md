# Agent Webhooks — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-webhooks`

## POST `/api/v1/agents/{agent_id}/webhooks/session`
**Create Session Webhook**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentWebhookCreateSession`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `prompt`: string | null
  - `session_mode`: "conversation" | "building"

**Response:** `AgentWebhookPublicWithToken`

---

## POST `/api/v1/agents/{agent_id}/webhooks/script`
**Create Script Webhook**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentWebhookCreateScript`):
  - `name`: string (required)
  - `type`: string
  - `payload_template`: string | null
  - `command`: string (required)
  - `command_timeout_seconds`: integer

**Response:** `AgentWebhookPublicWithToken`

---

## GET `/api/v1/agents/{agent_id}/webhooks`
**List Webhooks**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentWebhooksPublic`

---

## GET `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}`
**Get Webhook**

**Path parameters:**
- `agent_id`: uuid
- `webhook_pk`: uuid

**Response:** `AgentWebhookPublic`

---

## PATCH `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}`
**Update Webhook**

**Path parameters:**
- `agent_id`: uuid
- `webhook_pk`: uuid

**Request body** (`AgentWebhookUpdate`):
  - `name`: string | null
  - `enabled`: boolean | null
  - `payload_template`: string | null
  - `prompt`: string | null
  - `session_mode`: string | null
  - `command`: string | null
  - `command_timeout_seconds`: integer | null

**Response:** `AgentWebhookPublic`

---

## DELETE `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}`
**Delete Webhook**

**Path parameters:**
- `agent_id`: uuid
- `webhook_pk`: uuid

**Response:** `object`

---

## POST `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token`
**Regenerate Token**

**Path parameters:**
- `agent_id`: uuid
- `webhook_pk`: uuid

**Response:** `AgentWebhookPublicWithToken`

---

## GET `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}/logs`
**List Webhook Logs**

**Path parameters:**
- `agent_id`: uuid
- `webhook_pk`: uuid

**Query parameters:**
- `limit`: integer, default: `50`

**Response:** `AgentWebhookLogsPublic`

---
