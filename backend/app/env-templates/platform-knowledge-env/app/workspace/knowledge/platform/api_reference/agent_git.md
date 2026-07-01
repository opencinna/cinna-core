# Agent Git — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-git`

## POST `/api/v1/agents/checkout`
**Checkout Agent**

**Request body** (`AgentCheckoutRequest`):
  - `repo_url`: string (required)
  - `subdir`: string | null
  - `ref`: string
  - `ssh_key_id`: string | null
  - `sync_direction`: string
  - `name_override`: string | null

**Response:** `AgentCheckoutResponse`

---

## POST `/api/v1/agents/{agent_id}/git/connect`
**Connect Git Source**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentGitConnectRequest`):
  - `repo_url`: string (required)
  - `subdir`: string | null
  - `ref`: string
  - `ssh_key_id`: string | null
  - `sync_direction`: string
  - `commit_message`: string
  - `adopt_existing`: boolean

**Response:** `AgentGitSourcePublic`

---

## DELETE `/api/v1/agents/{agent_id}/git`
**Disconnect Git Source**

**Path parameters:**
- `agent_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agents/{agent_id}/git`
**Get Git Source**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentGitSourcePublic`

---

## GET `/api/v1/agents/{agent_id}/git/check-updates`
**Check Git Updates**

**Path parameters:**
- `agent_id`: uuid

**Response:** `GitUpdateStatus`

---

## GET `/api/v1/agents/{agent_id}/git/commits`
**List Git Commits**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `limit`: integer, default: `50`

**Response:** `GitCommitList`

---

## GET `/api/v1/agents/{agent_id}/git/dirty`
**Get Git Dirty**

**Path parameters:**
- `agent_id`: uuid

**Response:** `GitDirtyStatus`

---

## GET `/api/v1/agents/{agent_id}/git/status`
**Get Git Status**

**Path parameters:**
- `agent_id`: uuid

**Response:** `GitStatus`

---

## POST `/api/v1/agents/{agent_id}/git/pull`
**Pull Git Source**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPublic`

---

## POST `/api/v1/agents/{agent_id}/git/push`
**Push Git Source**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`GitPushRequest`):
  - `commit_message`: string (required)
  - `version`: string | null
  - `also_publish_bundle`: boolean

**Response:** `AgentGitSourcePublic`

---
