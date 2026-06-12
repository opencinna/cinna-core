# Mcp Connectors — API Reference

Auto-generated from OpenAPI spec. Tag: `mcp-connectors`

## POST `/api/v1/agents/{agent_id}/mcp-connectors`
**Create Mcp Connector**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`MCPConnectorCreate`):
  - `name`: string (required)
  - `mode`: string
  - `is_agent_to_agent`: boolean
  - `allowed_emails`: string[]
  - `allowed_user_ids`: uuid[]
  - `allow_token_access`: boolean
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
  - `is_agent_to_agent`: boolean | null
  - `allowed_emails`: array | null
  - `allowed_user_ids`: array | null
  - `allow_token_access`: boolean | null
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

## GET `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}/tokens`
**List Connector Tokens**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `MCPConnectorTokensPublic`

---

## POST `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}/tokens`
**Create Connector Token**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Request body** (`MCPConnectorTokenCreate`):
  - `label`: string (required)

**Response:** `MCPConnectorTokenCreated`

---

## PUT `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}/tokens/{token_id}`
**Update Connector Token**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid
- `token_id`: uuid

**Request body** (`MCPConnectorTokenUpdate`):
  - `revoked`: boolean | null

**Response:** `MCPConnectorTokenPublic`

---

## DELETE `/api/v1/agents/{agent_id}/mcp-connectors/{connector_id}/tokens/{token_id}`
**Delete Connector Token**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid
- `token_id`: uuid

**Response:** `Message`

---
