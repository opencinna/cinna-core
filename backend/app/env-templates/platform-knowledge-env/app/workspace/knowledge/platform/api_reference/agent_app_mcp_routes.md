# Agent App Mcp Routes — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-app-mcp-routes`

## GET `/api/v1/agents/{agent_id}/app-mcp-routes/`
**List Agent App Mcp Routes**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agents/{agent_id}/app-mcp-routes/`
**Create Agent App Mcp Route**

**Path parameters:**
- `agent_id`: uuid

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

## PUT `/api/v1/agents/{agent_id}/app-mcp-routes/{route_id}`
**Update Agent App Mcp Route**

**Path parameters:**
- `agent_id`: uuid
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

## DELETE `/api/v1/agents/{agent_id}/app-mcp-routes/{route_id}`
**Delete Agent App Mcp Route**

**Path parameters:**
- `agent_id`: uuid
- `route_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agents/{agent_id}/app-mcp-routes/conflicts`
**Check Route Conflicts**

**Path parameters:**
- `agent_id`: uuid

**Response:** `RouteConflictResponse`

---

## POST `/api/v1/agents/{agent_id}/app-mcp-routes/{route_id}/assignments`
**Assign Users To Agent Route**

**Path parameters:**
- `agent_id`: uuid
- `route_id`: uuid


---

## DELETE `/api/v1/agents/{agent_id}/app-mcp-routes/{route_id}/assignments/{user_id}`
**Remove User Assignment From Agent Route**

**Path parameters:**
- `agent_id`: uuid
- `route_id`: uuid
- `user_id`: uuid

**Response:** `Message`

---
