# Installs — API Reference

Auto-generated from OpenAPI spec. Tag: `installs`

## POST `/api/v1/agents/{agent_id}/publish`
**Publish Agent**

**Path parameters:**
- `agent_id`: uuid


**Response:** `AgentBundleRevisionPublic`

---

## POST `/api/v1/agents/{agent_id}/uninstall`
**Uninstall Install**

**Path parameters:**
- `agent_id`: uuid

**Response:** `object`

---

## POST `/api/v1/agents/{agent_id}/apply-update`
**Apply Update**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPublic`

---

## POST `/api/v1/agents/{agent_id}/check-updates`
**Check Updates**

**Path parameters:**
- `agent_id`: uuid

**Response:** `CheckUpdatesResponse`

---

## PATCH `/api/v1/agents/{agent_id}/update-mode`
**Set Update Mode**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`SetUpdateModeRequest`):
  - `update_mode`: string (required)

**Response:** `AgentPublic`

---

## PATCH `/api/v1/agents/{agent_id}/bundle-id`
**Edit Bundle Id**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`EditBundleIdRequest`):
  - `bundle_id`: string (required)

**Response:** `AgentPublic`

---
