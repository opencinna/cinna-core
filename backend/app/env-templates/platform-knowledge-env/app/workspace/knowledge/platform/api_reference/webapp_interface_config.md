# Webapp Interface Config — API Reference

Auto-generated from OpenAPI spec. Tag: `webapp-interface-config`

## GET `/api/v1/agents/{agent_id}/webapp-interface-config/`
**Get Webapp Interface Config**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentWebappInterfaceConfigPublic`

---

## PUT `/api/v1/agents/{agent_id}/webapp-interface-config/`
**Update Webapp Interface Config**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentWebappInterfaceConfigUpdate`):
  - `show_header`: boolean | null
  - `chat_mode`: string | null

**Response:** `AgentWebappInterfaceConfigPublic`

---
