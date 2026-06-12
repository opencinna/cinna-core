# Dashboards — API Reference

Auto-generated from OpenAPI spec. Tag: `Dashboards`

## GET `/api/v1/dashboards/agent-env-files`
**List Agent Env Files**

**Query parameters:**
- `agent_id`: string (required)
- `subfolder`: string, default: `files`

---

## GET `/api/v1/dashboards/`
**List Dashboards**

---

## POST `/api/v1/dashboards/`
**Create Dashboard**

**Request body** (`UserDashboardCreate`):
  - `name`: string (required)
  - `description`: string | null

**Response:** `UserDashboardPublic`

---

## GET `/api/v1/dashboards/{dashboard_id}`
**Get Dashboard**

**Path parameters:**
- `dashboard_id`: uuid

**Response:** `UserDashboardPublic`

---

## PUT `/api/v1/dashboards/{dashboard_id}`
**Update Dashboard**

**Path parameters:**
- `dashboard_id`: uuid

**Request body** (`UserDashboardUpdate`):
  - `name`: string | null
  - `description`: string | null
  - `sort_order`: integer | null

**Response:** `UserDashboardPublic`

---

## DELETE `/api/v1/dashboards/{dashboard_id}`
**Delete Dashboard**

**Path parameters:**
- `dashboard_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/dashboards/{dashboard_id}/blocks`
**Add Block**

**Path parameters:**
- `dashboard_id`: uuid

**Request body** (`UserDashboardBlockCreate`):
  - `agent_id`: uuid (required)
  - `view_type`: "webapp" | "latest_session" | "latest_tasks" | "agent_env_file"
  - `title`: string | null
  - `show_border`: boolean
  - `show_header`: boolean
  - `grid_x`: integer
  - `grid_y`: integer
  - `grid_w`: integer
  - `grid_h`: integer
  - `config`: object | null

**Response:** `UserDashboardBlockPublic`

---

## PUT `/api/v1/dashboards/{dashboard_id}/blocks/layout`
**Update Block Layout**

**Path parameters:**
- `dashboard_id`: uuid


---

## PUT `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}`
**Update Block**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`UserDashboardBlockUpdate`):
  - `view_type`: string | null
  - `title`: string | null
  - `show_border`: boolean | null
  - `show_header`: boolean | null
  - `grid_x`: integer | null
  - `grid_y`: integer | null
  - `grid_w`: integer | null
  - `grid_h`: integer | null
  - `config`: object | null

**Response:** `UserDashboardBlockPublic`

---

## DELETE `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}`
**Delete Block**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/prompt-actions`
**List Prompt Actions**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

---

## POST `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/prompt-actions`
**Create Prompt Action**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Request body** (`UserDashboardBlockPromptActionCreate`):
  - `prompt_text`: string (required)
  - `label`: string | null
  - `sort_order`: integer

**Response:** `UserDashboardBlockPromptActionPublic`

---

## PUT `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}`
**Update Prompt Action**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid
- `action_id`: uuid

**Request body** (`UserDashboardBlockPromptActionUpdate`):
  - `prompt_text`: string | null
  - `label`: string | null
  - `sort_order`: integer | null

**Response:** `UserDashboardBlockPromptActionPublic`

---

## DELETE `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}`
**Delete Prompt Action**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid
- `action_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/latest-session`
**Get Block Latest Session**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Response:** `SessionPublic`

---

## GET `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/env-files`
**List Block Env Files**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Query parameters:**
- `subfolder`: string, default: `files`

---

## GET `/api/v1/dashboards/{dashboard_id}/blocks/{block_id}/env-file`
**Get Block Env File**

**Path parameters:**
- `dashboard_id`: uuid
- `block_id`: uuid

**Query parameters:**
- `path`: string (required)

---
