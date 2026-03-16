# Knowledge Sources — API Reference

Auto-generated from OpenAPI spec. Tag: `knowledge-sources`

## GET `/api/v1/knowledge-sources/`
**List Knowledge Sources**

**Query parameters:**
- `workspace_id`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

---

## POST `/api/v1/knowledge-sources/`
**Create Knowledge Source**

**Request body** (`AIKnowledgeGitRepoCreate`):
  - `name`: string (required)
  - `description`: string | null
  - `git_url`: string (required)
  - `branch`: string
  - `ssh_key_id`: string | null
  - `workspace_access_type`: WorkspaceAccessType
  - `workspace_ids`: array | null

**Response:** `AIKnowledgeGitRepoPublic`

---

## GET `/api/v1/knowledge-sources/{source_id}`
**Get Knowledge Source**

**Path parameters:**
- `source_id`: uuid

**Response:** `AIKnowledgeGitRepoPublic`

---

## PUT `/api/v1/knowledge-sources/{source_id}`
**Update Knowledge Source**

**Path parameters:**
- `source_id`: uuid

**Request body** (`AIKnowledgeGitRepoUpdate`):
  - `name`: string | null
  - `description`: string | null
  - `branch`: string | null
  - `ssh_key_id`: string | null
  - `is_enabled`: boolean | null
  - `workspace_access_type`: WorkspaceAccessType | null
  - `workspace_ids`: array | null
  - `public_discovery`: boolean | null

**Response:** `AIKnowledgeGitRepoPublic`

---

## DELETE `/api/v1/knowledge-sources/{source_id}`
**Delete Knowledge Source**

**Path parameters:**
- `source_id`: uuid

---

## POST `/api/v1/knowledge-sources/{source_id}/enable`
**Enable Knowledge Source**

**Path parameters:**
- `source_id`: uuid

**Response:** `AIKnowledgeGitRepoPublic`

---

## POST `/api/v1/knowledge-sources/{source_id}/disable`
**Disable Knowledge Source**

**Path parameters:**
- `source_id`: uuid

**Response:** `AIKnowledgeGitRepoPublic`

---

## POST `/api/v1/knowledge-sources/{source_id}/check-access`
**Check Knowledge Source Access**

**Path parameters:**
- `source_id`: uuid

**Response:** `CheckAccessResponse`

---

## POST `/api/v1/knowledge-sources/{source_id}/refresh`
**Refresh Knowledge Source**

**Path parameters:**
- `source_id`: uuid

**Response:** `RefreshKnowledgeResponse`

---

## GET `/api/v1/knowledge-sources/{source_id}/articles`
**List Knowledge Articles**

**Path parameters:**
- `source_id`: uuid

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

---

## GET `/api/v1/knowledge-sources/discoverable/list`
**List Discoverable Sources**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

---

## POST `/api/v1/knowledge-sources/discoverable/{source_id}/enable`
**Enable Discoverable Source**

**Path parameters:**
- `source_id`: uuid

---

## POST `/api/v1/knowledge-sources/discoverable/{source_id}/disable`
**Disable Discoverable Source**

**Path parameters:**
- `source_id`: uuid

---
