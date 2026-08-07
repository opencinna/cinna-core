# Agent Api — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-api`

## GET `/api/v1/agents/{agent_id}/agent-api/_status`
**Get Agent Api Status**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agents/{agent_id}/agent-api/_refresh`
**Refresh Agent Api Status**

**Path parameters:**
- `agent_id`: uuid

---

## GET `/api/v1/agents/{agent_id}/agent-api/openapi.json`
**Get Agent Api Spec**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agents/{agent_id}/agent-api/connect`
**Connect Agent Api**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ConnectAgentApiRequest`):
  - `credential_label`: string | null
  - `read_only_override`: boolean
  - `consumer_agent_id`: string | null

**Response:** `ConnectAgentApiResponse`

---

## GET `/api/v1/agents/{agent_id}/agent-api/connections`
**List Agent Api Connections**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentApiProducerConnections`

---

## DELETE `/api/v1/agents/{agent_id}/agent-api/connections/{token_id}`
**Delete Agent Api Connection**

**Path parameters:**
- `agent_id`: uuid
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agents/{agent_id}/agent-api/grants/scope-catalog`
**Get Agent Api Scope Catalog**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentApiScopeCatalog`

---

## GET `/api/v1/agents/{agent_id}/agent-api/grants`
**List Agent Api Grants**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentApiAccessGrantsPublic`

---

## POST `/api/v1/agents/{agent_id}/agent-api/grants`
**Create Agent Api Grant**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentApiAccessGrantCreate`):
  - `scopes`: string[]
  - `user_id`: uuid (required)

**Response:** `AgentApiAccessGrantPublic`

---

## PUT `/api/v1/agents/{agent_id}/agent-api/grants/{grant_id}`
**Update Agent Api Grant**

**Path parameters:**
- `agent_id`: uuid
- `grant_id`: uuid

**Request body** (`AgentApiAccessGrantUpdate`):
  - `scopes`: array | null

**Response:** `AgentApiAccessGrantPublic`

---

## DELETE `/api/v1/agents/{agent_id}/agent-api/grants/{grant_id}`
**Delete Agent Api Grant**

**Path parameters:**
- `agent_id`: uuid
- `grant_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/{agent_id}/agent-api/keys`
**Create Agent Api Key**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentApiKeyCreate`):
  - `label`: string | null
  - `subject_user_id`: uuid (required)
  - `scopes`: array | null
  - `read_only_override`: boolean
  - `expires_in_days`: integer | null

**Response:** `AgentApiKeyCreated`

---

## GET `/api/v1/agents/{agent_id}/agent-api/keys`
**List Agent Api Keys**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentApiKeysPublic`

---

## DELETE `/api/v1/agents/{agent_id}/agent-api/keys/{key_id}`
**Delete Agent Api Key**

**Path parameters:**
- `agent_id`: uuid
- `key_id`: uuid

**Response:** `Message`

---
