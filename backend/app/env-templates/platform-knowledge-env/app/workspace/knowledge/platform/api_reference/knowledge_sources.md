# Knowledge Sources — API Reference

Auto-generated from OpenAPI spec. Tag: `knowledge-sources`

## GET `/api/v1/knowledge-sources/`
**List Knowledge Sources**

**Query parameters:**
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
  - `access_level`: KnowledgeSourceAccessLevel

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
  - `access_level`: KnowledgeSourceAccessLevel | null

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

## GET `/api/v1/knowledge-sources/{source_id}/articles/{article_id}`
**Get Knowledge Article**

**Path parameters:**
- `source_id`: uuid
- `article_id`: uuid

**Response:** `KnowledgeArticleDetail`

---

## GET `/api/v1/knowledge-sources/{source_id}/export`
**Export Knowledge Source**

**Path parameters:**
- `source_id`: uuid

---

## GET `/api/v1/knowledge-sources/{source_id}/shared-users`
**List Knowledge Source Shared Users**

**Path parameters:**
- `source_id`: uuid

---

## POST `/api/v1/knowledge-sources/{source_id}/shared-users`
**Add Knowledge Source Shared User**

**Path parameters:**
- `source_id`: uuid

**Request body** (`KnowledgeSourceShareCreate`):
  - `user_id`: uuid (required)

**Response:** `KnowledgeSourceSharedUserPublic`

---

## DELETE `/api/v1/knowledge-sources/{source_id}/shared-users/{user_id}`
**Remove Knowledge Source Shared User**

**Path parameters:**
- `source_id`: uuid
- `user_id`: uuid

---

## GET `/api/v1/knowledge-sources/{source_id}/shared-groups`
**List Knowledge Source Shared Groups**

**Path parameters:**
- `source_id`: uuid

---

## POST `/api/v1/knowledge-sources/{source_id}/shared-groups`
**Add Knowledge Source Shared Group**

**Path parameters:**
- `source_id`: uuid

**Request body** (`KnowledgeSourceGroupShareCreate`):
  - `group_id`: uuid (required)

**Response:** `KnowledgeSourceSharedGroupPublic`

---

## DELETE `/api/v1/knowledge-sources/{source_id}/shared-groups/{group_id}`
**Remove Knowledge Source Shared Group**

**Path parameters:**
- `source_id`: uuid
- `group_id`: uuid

---
