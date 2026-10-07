# Acp Connectors — API Reference

Auto-generated from OpenAPI spec. Tag: `acp-connectors`

## POST `/api/v1/agents/{agent_id}/acp-connectors`
**Create Acp Connector**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ACPConnectorCreate`):
  - `name`: string (required)
  - `mode`: "conversation" | "building"
  - `is_active`: boolean
  - `max_connections`: integer

**Response:** `ACPConnectorPublic`

---

## GET `/api/v1/agents/{agent_id}/acp-connectors`
**List Acp Connectors**

**Path parameters:**
- `agent_id`: uuid

**Response:** `ACPConnectorsPublic`

---

## GET `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}`
**Get Acp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `ACPConnectorPublic`

---

## PUT `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}`
**Update Acp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Request body** (`ACPConnectorUpdate`):
  - `name`: string | null
  - `mode`: string | null
  - `is_active`: boolean | null
  - `max_connections`: integer | null

**Response:** `ACPConnectorPublic`

---

## DELETE `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}`
**Delete Acp Connector**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens`
**Create Acp Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Request body** (`ACPTokenCreate`):
  - `label`: string (required)
  - `expires_in_days`: integer

**Response:** `ACPTokenCreated`

---

## GET `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens`
**List Acp Tokens**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid

**Response:** `ACPTokensPublic`

---

## POST `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens/{token_id}/revoke`
**Revoke Acp Token**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid
- `token_id`: uuid

**Response:** `ACPTokenPublic`

---

## DELETE `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens/{token_id}`
**Delete Acp Token**

**Path parameters:**
- `agent_id`: uuid
- `connector_id`: uuid
- `token_id`: uuid

**Response:** `Message`

---
