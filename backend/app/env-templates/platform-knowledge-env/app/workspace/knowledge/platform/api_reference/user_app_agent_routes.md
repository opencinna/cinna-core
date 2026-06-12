# User App Agent Routes — API Reference

Auto-generated from OpenAPI spec. Tag: `user-app-agent-routes`

## GET `/api/v1/users/me/app-agent-routes/`
**List User App Agent Routes**

**Response:** `UserAppAgentRoutesResponse`

---

## POST `/api/v1/users/me/app-agent-routes/`
**Create User App Agent Route**

**Request body** (`UserAppAgentRouteCreate`):
  - `agent_id`: uuid (required)
  - `session_mode`: string
  - `trigger_prompt`: string (required)
  - `message_patterns`: string | null
  - `channel_app_mcp`: boolean
  - `is_active`: boolean

**Response:** `UserAppAgentRoutePublic`

---

## PUT `/api/v1/users/me/app-agent-routes/{route_id}`
**Update User App Agent Route**

**Path parameters:**
- `route_id`: uuid

**Request body** (`UserAppAgentRouteUpdate`):
  - `session_mode`: string | null
  - `trigger_prompt`: string | null
  - `message_patterns`: string | null
  - `channel_app_mcp`: boolean | null
  - `is_active`: boolean | null

**Response:** `UserAppAgentRoutePublic`

---

## DELETE `/api/v1/users/me/app-agent-routes/{route_id}`
**Delete User App Agent Route**

**Path parameters:**
- `route_id`: uuid

**Response:** `Message`

---

## PATCH `/api/v1/users/me/app-agent-routes/admin-assignments/{assignment_id}`
**Toggle Admin Assignment**

**Path parameters:**
- `assignment_id`: uuid

**Query parameters:**
- `is_enabled`: boolean (required)

**Response:** `AppAgentRouteAssignmentPublic`

---
