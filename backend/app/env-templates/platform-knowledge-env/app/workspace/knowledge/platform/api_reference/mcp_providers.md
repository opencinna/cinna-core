# Mcp Providers — API Reference

Auto-generated from OpenAPI spec. Tag: `mcp-providers`

## GET `/api/v1/mcp-providers/discoverable-agents`
**List Discoverable Agents**

**Query parameters:**
- `consumer_agent_id`: string | null

**Response:** `DiscoverableAgents`

---

## POST `/api/v1/mcp-providers/connect/agent`
**Connect Agent**

**Request body** (`ConnectMcpProviderAgentRequest`):
  - `connector_id`: uuid (required)
  - `consumer_agent_id`: string | null
  - `mcp_mode_conversation`: boolean
  - `mcp_mode_building`: boolean
  - `label`: string | null

**Response:** `MCPProviderConnectionResponse`

---

## POST `/api/v1/mcp-providers/connect/external`
**Connect External**

**Request body** (`ConnectMcpProviderExternalRequest`):
  - `endpoint_url`: string (required)
  - `transport`: string
  - `auth_mode`: string
  - `token`: string | null
  - `consumer_agent_id`: string | null
  - `mcp_mode_conversation`: boolean
  - `mcp_mode_building`: boolean
  - `label`: string | null

**Response:** `MCPProviderConnectionResponse`

---

## GET `/api/v1/mcp-providers/{credential_id}/status`
**Get Provider Status**

**Path parameters:**
- `credential_id`: uuid

**Response:** `MCPProviderStatus`

---

## GET `/api/v1/mcp-providers/{credential_id}/oauth/authorize`
**Oauth Authorize**

**Path parameters:**
- `credential_id`: uuid

**Response:** `MCPProviderOAuthAuthorizeResponse`

---

## POST `/api/v1/mcp-providers/{credential_id}/oauth/reauthorize`
**Oauth Reauthorize**

**Path parameters:**
- `credential_id`: uuid

**Response:** `MCPProviderOAuthAuthorizeResponse`

---

## POST `/api/v1/mcp-providers/oauth/callback`
**Oauth Callback**

**Request body** (`MCPProviderOAuthCallbackRequest`):
  - `code`: string (required)
  - `state`: string (required)

**Response:** `MCPProviderOAuthCallbackResponse`

---

## POST `/api/v1/mcp-providers/{credential_id}/test`
**Test Connection**

**Path parameters:**
- `credential_id`: uuid

**Response:** `MCPProviderTestResult`

---
