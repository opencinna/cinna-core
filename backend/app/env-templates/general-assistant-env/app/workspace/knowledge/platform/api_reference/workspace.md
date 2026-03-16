# Workspace — API Reference

Auto-generated from OpenAPI spec. Tag: `workspace`

## GET `/api/v1/environments/{env_id}/workspace/tree`
**Get Workspace Tree**

**Path parameters:**
- `env_id`: uuid

---

## GET `/api/v1/environments/{env_id}/workspace/download/{path}`
**Download Workspace Item**

**Path parameters:**
- `env_id`: uuid
- `path`: string

---

## GET `/api/v1/environments/{env_id}/workspace/view-file/{path}`
**View Workspace File**

**Path parameters:**
- `env_id`: uuid
- `path`: string

---

## GET `/api/v1/environments/{env_id}/database/tables/{path}`
**Get Database Tables**

**Path parameters:**
- `env_id`: uuid
- `path`: string

---

## GET `/api/v1/environments/{env_id}/database/schema/{path}`
**Get Database Schema**

**Path parameters:**
- `env_id`: uuid
- `path`: string

---

## POST `/api/v1/environments/{env_id}/database/query`
**Execute Database Query**

**Path parameters:**
- `env_id`: uuid

**Request body** (`DatabaseQueryRequest`):
  - `path`: string (required)
  - `query`: string (required)
  - `page`: integer | null
  - `page_size`: integer | null
  - `timeout_seconds`: integer

---

## POST `/api/v1/environments/{env_id}/database/generate-sql`
**Generate Sql Query**

**Path parameters:**
- `env_id`: uuid

**Request body** (`GenerateSQLRequest`):
  - `path`: string (required)
  - `user_request`: string (required)
  - `current_query`: string | null

---
