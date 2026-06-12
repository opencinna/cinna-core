# Identity — API Reference

Auto-generated from OpenAPI spec. Tag: `identity`

## GET `/api/v1/identity/bindings/`
**List Identity Bindings**

---

## POST `/api/v1/identity/bindings/`
**Create Identity Binding**

**Request body** (`IdentityAgentBindingCreate`):
  - `agent_id`: uuid (required)
  - `trigger_prompt`: string (required)
  - `message_patterns`: string | null
  - `prompt_examples`: string | null
  - `session_mode`: string
  - `assigned_user_ids`: uuid[]
  - `auto_enable`: boolean

**Response:** `IdentityAgentBindingPublic`

---

## PUT `/api/v1/identity/bindings/{binding_id}`
**Update Identity Binding**

**Path parameters:**
- `binding_id`: uuid

**Request body** (`IdentityAgentBindingUpdate`):
  - `trigger_prompt`: string | null
  - `message_patterns`: string | null
  - `prompt_examples`: string | null
  - `session_mode`: string | null
  - `is_active`: boolean | null

**Response:** `IdentityAgentBindingPublic`

---

## DELETE `/api/v1/identity/bindings/{binding_id}`
**Delete Identity Binding**

**Path parameters:**
- `binding_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/identity/bindings/{binding_id}/assignments`
**Assign Users To Binding**

**Path parameters:**
- `binding_id`: uuid


---

## DELETE `/api/v1/identity/bindings/{binding_id}/assignments/{user_id}`
**Remove User Assignment**

**Path parameters:**
- `binding_id`: uuid
- `user_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/identity/summary/`
**Get Identity Summary**

---
