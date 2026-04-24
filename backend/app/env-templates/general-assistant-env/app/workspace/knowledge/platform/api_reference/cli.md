# Cli — API Reference

Auto-generated from OpenAPI spec. Tag: `cli`

## POST `/api/v1/cli/setup-tokens`
**Create Setup Token**

**Request body** (`CLISetupTokenCreate`):
  - `agent_id`: uuid (required)

**Response:** `CLISetupTokenCreated`

---

## GET `/api/v1/cli/tokens`
**List Cli Tokens**

**Query parameters:**
- `agent_id`: string | null

**Response:** `CLITokensPublic`

---

## DELETE `/api/v1/cli/tokens/{token_id}`
**Revoke Cli Token**

**Path parameters:**
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/agents/{agent_id}/building-context`
**Get Building Context**

**Path parameters:**
- `agent_id`: uuid

---

## GET `/api/v1/cli/agents/{agent_id}/workspace`
**Get Workspace**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/knowledge/search`
**Search Knowledge**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`KnowledgeSearchBody`):
  - `query`: string (required)
  - `topic`: string | null

---

## GET `/api/v1/cli/agents/{agent_id}/sync-runtime`
**Get Sync Runtime**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/exec`
**Exec Command**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ExecBody`):
  - `command`: string (required)

---

## GET `/api/cli-setup/{token}`
**Get Bootstrap Script**

**Path parameters:**
- `token`: string

---

## POST `/api/cli-setup/{token}`
**Exchange Setup Token**

**Path parameters:**
- `token`: string

**Request body** (`ExchangeSetupTokenBody`):
  - `machine_name`: string
  - `machine_info`: string | null

---
