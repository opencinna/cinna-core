# Admin Environments — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-environments`

## GET `/api/v1/admin/agent-environments/`
**List Admin Environments**

**Query parameters:**
- `template`: string | null
- `status`: string | null
- `is_stale`: boolean | null
- `in_use`: boolean | null
- `update_available`: boolean | null
- `owner_id`: string | null
- `search`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `AdminAgentEnvironmentsPublic`

---

## POST `/api/v1/admin/agent-environments/bulk-rebuild`
**Bulk Rebuild Environments**

**Request body** (`AdminBulkRebuildRequest`):
  - `environment_ids`: uuid[] (required)

**Response:** `AdminBulkRebuildResponse`

---

## POST `/api/v1/admin/agent-environments/{env_id}/rebuild`
**Rebuild Single Environment**

**Path parameters:**
- `env_id`: uuid

**Response:** `Message`

---
