# Admin User Groups — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-user-groups`

## GET `/api/v1/admin/user-groups/`
**List User Groups**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `q`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `UserGroupsPublic`

---

## POST `/api/v1/admin/user-groups/`
**Create User Group**

> Not available via `cinna api` (excluded_path).

**Request body** (`UserGroupCreate`):
  - `name`: string (required)
  - `description`: string | null

**Response:** `UserGroupPublic`

---

## GET `/api/v1/admin/user-groups/{group_id}`
**Get User Group**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Response:** `UserGroupPublic`

---

## PATCH `/api/v1/admin/user-groups/{group_id}`
**Update User Group**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Request body** (`UserGroupUpdate`):
  - `name`: string | null
  - `description`: string | null

**Response:** `UserGroupPublic`

---

## DELETE `/api/v1/admin/user-groups/{group_id}`
**Delete User Group**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/admin/user-groups/{group_id}/members`
**List User Group Members**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Query parameters:**
- `q`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `UserGroupMembersPublic`

---

## POST `/api/v1/admin/user-groups/{group_id}/members`
**Add User Group Members**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Request body** (`UserGroupMembersAdd`):
  - `user_ids`: uuid[] (required)

**Response:** `UserGroupMembersPublic`

---

## DELETE `/api/v1/admin/user-groups/{group_id}/members/{user_id}`
**Remove User Group Member**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid
- `user_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/admin/user-groups/{group_id}/members/{user_id}/removal-impact`
**Get User Group Member Removal Impact**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid
- `user_id`: uuid

**Response:** `UserGroupImpact`

---

## GET `/api/v1/admin/user-groups/{group_id}/delete-impact`
**Get User Group Delete Impact**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Response:** `UserGroupImpact`

---

## GET `/api/v1/admin/user-groups/{group_id}/shares`
**List User Group Shares**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid

**Response:** `UserGroupSharesPublic`

---

## GET `/api/v1/admin/user-groups/{group_id}/shares/{resource_type}/{resource_id}/revoke-impact`
**Get User Group Share Revoke Impact**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid
- `resource_type`: "agent_bundle" | "desktop_agent" | "skill_package" | "credential" | "ai_credential" | "artifact" | "knowledge_source" | "agent_api_scopes"
- `resource_id`: uuid

**Response:** `UserGroupImpact`

---

## DELETE `/api/v1/admin/user-groups/{group_id}/shares/{resource_type}/{resource_id}`
**Revoke User Group Share**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `group_id`: uuid
- `resource_type`: "agent_bundle" | "desktop_agent" | "skill_package" | "credential" | "ai_credential" | "artifact" | "knowledge_source" | "agent_api_scopes"
- `resource_id`: uuid

**Response:** `Message`

---
