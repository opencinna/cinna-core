# Access Tokens — API Reference

Auto-generated from OpenAPI spec. Tag: `access-tokens`

## GET `/api/v1/agents/{agent_id}/access-tokens/`
**List Access Tokens**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentAccessTokensPublic`

---

## POST `/api/v1/agents/{agent_id}/access-tokens/`
**Create Access Token**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentAccessTokenCreate`):
  - `name`: string (required)
  - `mode`: AccessTokenMode
  - `scope`: AccessTokenScope
  - `agent_id`: uuid (required)

**Response:** `AgentAccessTokenCreated`

---

## GET `/api/v1/agents/{agent_id}/access-tokens/{token_id}`
**Get Access Token**

**Path parameters:**
- `agent_id`: uuid
- `token_id`: uuid

**Response:** `AgentAccessTokenPublic`

---

## PUT `/api/v1/agents/{agent_id}/access-tokens/{token_id}`
**Update Access Token**

**Path parameters:**
- `agent_id`: uuid
- `token_id`: uuid

**Request body** (`AgentAccessTokenUpdate`):
  - `name`: string | null
  - `is_revoked`: boolean | null

**Response:** `AgentAccessTokenPublic`

---

## DELETE `/api/v1/agents/{agent_id}/access-tokens/{token_id}`
**Delete Access Token**

**Path parameters:**
- `agent_id`: uuid
- `token_id`: uuid

**Response:** `Message`

---
