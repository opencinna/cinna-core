# App Agent Routes — API Reference

Auto-generated from OpenAPI spec. Tag: `app-agent-routes`

## GET `/api/v1/admin/app-agent-routes/`
**List App Agent Routes**

---

## POST `/api/v1/admin/app-agent-routes/`
**Create App Agent Route**

**Request body** (`AppAgentRouteCreate`):
  - `name`: string (required)
  - `agent_id`: uuid (required)
  - `session_mode`: string
  - `trigger_prompt`: string (required)
  - `message_patterns`: string | null
  - `prompt_examples`: string | null
  - `channel_app_mcp`: boolean
  - `is_active`: boolean
  - `auto_enable_for_users`: boolean
  - `activate_for_myself`: boolean
  - `assigned_user_ids`: uuid[]

**Response:** `AppAgentRoutePublic`

---

## GET `/api/v1/admin/app-agent-routes/{route_id}`
**Get App Agent Route**

**Path parameters:**
- `route_id`: uuid

**Response:** `AppAgentRoutePublic`

---

## PUT `/api/v1/admin/app-agent-routes/{route_id}`
**Update App Agent Route**

**Path parameters:**
- `route_id`: uuid

**Request body** (`AppAgentRouteUpdate`):
  - `name`: string | null
  - `session_mode`: string | null
  - `trigger_prompt`: string | null
  - `message_patterns`: string | null
  - `prompt_examples`: string | null
  - `channel_app_mcp`: boolean | null
  - `is_active`: boolean | null
  - `auto_enable_for_users`: boolean | null

**Response:** `AppAgentRoutePublic`

---

## DELETE `/api/v1/admin/app-agent-routes/{route_id}`
**Delete App Agent Route**

**Path parameters:**
- `route_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/admin/app-agent-routes/{route_id}/assignments`
**Assign Users To Route**

**Path parameters:**
- `route_id`: uuid


---

## DELETE `/api/v1/admin/app-agent-routes/{route_id}/assignments/{user_id}`
**Remove User Assignment**

**Path parameters:**
- `route_id`: uuid
- `user_id`: uuid

**Response:** `Message`

---
