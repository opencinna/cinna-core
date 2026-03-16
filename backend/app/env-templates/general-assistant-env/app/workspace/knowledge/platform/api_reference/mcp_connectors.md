# Mcp Connectors — API Reference

Auto-generated from OpenAPI spec. Tag: `mcp-connectors`

## POST `/api/v1/agents/{agent_id}/mcp-connectors`
**Create Mcp Connector**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`MCPConnectorCreate`):
  - `name`: string (required)
  - `mode`: string
  - `allowed_emails`: string[]
  - `max_clients`: integer

**Response:** `MCPConnectorPublic`

---

## GET `/api/v1/agents/{agent_id}/mcp-connectors`
**List Mcp Connectors**

**Path parameters:**
- `agent_id`: uuid

**Response:** `MCPConnectorsPublic`

---

## GET `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}`
**Get Mcp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `MCPConnectorPublic`

---

## PUT `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}`
**Update Mcp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Request body** (`MCPConnectorUpdate`):
  - `name`: string | null
  - `mode`: string | null
  - `is_active`: boolean | null
  - `allowed_emails`: array | null
  - `max_clients`: integer | null

**Response:** `MCPConnectorPublic`

---

## DELETE `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}`
**Delete Mcp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `Message`

---
