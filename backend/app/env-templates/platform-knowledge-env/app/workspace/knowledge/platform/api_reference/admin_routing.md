# Admin Routing — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-routing`

## GET `/api/v1/admin/routing/traces`
**List Routing Traces**

**Query parameters:**
- `channel_id`: string | null
- `origin`: string | null
- `outcome`: string | null
- `user_id`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `50`

**Response:** `RoutingDecisionsPublic`

---

## DELETE `/api/v1/admin/routing/traces`
**Clear Routing Traces**

**Query parameters:**
- `channel_id`: string | null
- `all`: boolean, default: `False`

**Response:** `Message`

---

## GET `/api/v1/admin/routing/traces/{trace_id}`
**Get Routing Trace**

**Path parameters:**
- `trace_id`: uuid

**Query parameters:**
- `expected_agent_id`: string | null

**Response:** `RoutingDecisionPublic`

---

## POST `/api/v1/admin/routing/simulate`
**Simulate Routing**

**Request body** (`RoutingSimulateRequest`):
  - `message`: string (required)
  - `as_user_id`: uuid (required)
  - `channel_id`: string | null
  - `include_catalog`: boolean

**Response:** `RoutingDecisionPublic`

---

## POST `/api/v1/admin/routing/traces/{trace_id}/replay`
**Replay Routing Trace**

**Path parameters:**
- `trace_id`: uuid


**Response:** `RoutingReplayResult`

---

## POST `/api/v1/admin/routing/traces/{trace_id}/recommendation`
**Draft Routing Recommendation**

**Path parameters:**
- `trace_id`: uuid


**Response:** `RoutingRecommendationPublic`

---
