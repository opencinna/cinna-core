# Environments — API Reference

Auto-generated from OpenAPI spec. Tag: `environments`

## GET `/api/v1/environments/{id}`
**Get Environment**

**Path parameters:**
- `id`: uuid

**Response:** `AgentEnvironmentPublic`

---

## PATCH `/api/v1/environments/{id}`
**Update Environment**

**Path parameters:**
- `id`: uuid

**Request body** (`AgentEnvironmentUpdate`):
  - `instance_name`: string | null
  - `config`: object | null

**Response:** `AgentEnvironmentPublic`

---

## DELETE `/api/v1/environments/{id}`
**Delete Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/reconfigure`
**Reconfigure Environment**

**Path parameters:**
- `id`: uuid

**Request body** (`AgentEnvironmentReconfigure`):
  - `agent_sdk_conversation`: string | null
  - `agent_sdk_building`: string | null
  - `model_override_conversation`: string | null
  - `model_override_building`: string | null
  - `use_default_ai_credentials`: boolean
  - `conversation_ai_credential_id`: string | null
  - `building_ai_credential_id`: string | null
  - `rebuild`: boolean

**Response:** `AgentEnvironmentPublic`

---

## POST `/api/v1/environments/{id}/start`
**Start Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/stop`
**Stop Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/suspend`
**Suspend Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/restart`
**Restart Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/rebuild`
**Rebuild Environment**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## GET `/api/v1/environments/{id}/status`
**Get Environment Status**

**Path parameters:**
- `id`: uuid

**Response:** `object`

---

## GET `/api/v1/environments/{id}/health`
**Check Environment Health**

**Path parameters:**
- `id`: uuid

**Response:** `object`

---

## GET `/api/v1/environments/{id}/logs`
**Get Environment Logs**

**Path parameters:**
- `id`: uuid

**Query parameters:**
- `lines`: integer, default: `100`

**Response:** `object`

---

## POST `/api/v1/environments/{id}/workspace-files-changed`
**Workspace Files Changed**

**Path parameters:**
- `id`: uuid


**Response:** `Message`

---

## POST `/api/v1/environments/{id}/prompt-file-changed`
**Prompt File Changed**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/environments/{id}/agent-api-reloaded`
**Agent Api Reloaded**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---
