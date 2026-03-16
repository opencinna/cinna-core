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
