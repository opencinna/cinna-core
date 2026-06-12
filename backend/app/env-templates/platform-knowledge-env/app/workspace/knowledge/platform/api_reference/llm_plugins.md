# Llm Plugins — API Reference

Auto-generated from OpenAPI spec. Tag: `llm-plugins`

## POST `/api/v1/llm-plugins/marketplaces`
**Create Marketplace**

**Request body** (`LLMPluginMarketplaceCreate`):
  - `url`: string (required)
  - `git_branch`: string
  - `ssh_key_id`: string | null
  - `public_discovery`: boolean
  - `type`: string

**Response:** `LLMPluginMarketplacePublic`

---

## GET `/api/v1/llm-plugins/marketplaces`
**List Marketplaces**

**Query parameters:**
- `include_public`: boolean, default: `True`

**Response:** `LLMPluginMarketplacesPublic`

---

## GET `/api/v1/llm-plugins/marketplaces/{marketplace_id}`
**Get Marketplace**

**Path parameters:**
- `marketplace_id`: uuid

**Response:** `LLMPluginMarketplacePublic`

---

## PUT `/api/v1/llm-plugins/marketplaces/{marketplace_id}`
**Update Marketplace**

**Path parameters:**
- `marketplace_id`: uuid

**Request body** (`LLMPluginMarketplaceUpdate`):
  - `name`: string | null
  - `description`: string | null
  - `owner_name`: string | null
  - `owner_email`: string | null
  - `url`: string | null
  - `git_branch`: string | null
  - `ssh_key_id`: string | null
  - `public_discovery`: boolean | null
  - `type`: string | null

**Response:** `LLMPluginMarketplacePublic`

---

## DELETE `/api/v1/llm-plugins/marketplaces/{marketplace_id}`
**Delete Marketplace**

**Path parameters:**
- `marketplace_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/llm-plugins/marketplaces/{marketplace_id}/sync`
**Sync Marketplace**

**Path parameters:**
- `marketplace_id`: uuid

**Response:** `LLMPluginMarketplacePublic`

---

## GET `/api/v1/llm-plugins/discover`
**Discover Plugins**

**Query parameters:**
- `search`: string | null
- `category`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `30`

**Response:** `LLMPluginMarketplacePluginsPublic`

---

## GET `/api/v1/llm-plugins/plugins/{plugin_id}`
**Get Plugin**

**Path parameters:**
- `plugin_id`: uuid

**Response:** `LLMPluginMarketplacePluginPublic`

---

## GET `/api/v1/llm-plugins/agents/{agent_id}/plugins`
**List Agent Plugins**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPluginLinksPublic`

---

## POST `/api/v1/llm-plugins/agents/{agent_id}/plugins`
**Install Agent Plugin**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentPluginLinkCreate`):
  - `plugin_id`: uuid (required)
  - `conversation_mode`: boolean
  - `building_mode`: boolean
  - `disabled`: boolean

**Response:** `PluginSyncResponse`

---

## DELETE `/api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}`
**Uninstall Agent Plugin**

**Path parameters:**
- `agent_id`: uuid
- `link_id`: uuid

**Response:** `PluginSyncResponse`

---

## PUT `/api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}`
**Update Agent Plugin**

**Path parameters:**
- `agent_id`: uuid
- `link_id`: uuid

**Request body** (`AgentPluginLinkUpdate`):
  - `conversation_mode`: boolean | null
  - `building_mode`: boolean | null
  - `disabled`: boolean | null

**Response:** `PluginSyncResponse`

---

## POST `/api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}/upgrade`
**Upgrade Agent Plugin**

**Path parameters:**
- `agent_id`: uuid
- `link_id`: uuid

**Response:** `PluginSyncResponse`

---
