# User Workspaces — API Reference

Auto-generated from OpenAPI spec. Tag: `user-workspaces`

## GET `/api/v1/user-workspaces/`
**Read Workspaces**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `UserWorkspacesPublic`

---

## POST `/api/v1/user-workspaces/`
**Create Workspace**

**Request body** (`UserWorkspaceCreate`):
  - `name`: string (required)
  - `icon`: string | null

**Response:** `UserWorkspacePublic`

---

## GET `/api/v1/user-workspaces/{workspace_id}`
**Read Workspace**

**Path parameters:**
- `workspace_id`: uuid

**Response:** `UserWorkspacePublic`

---

## PUT `/api/v1/user-workspaces/{workspace_id}`
**Update Workspace**

**Path parameters:**
- `workspace_id`: uuid

**Request body** (`UserWorkspaceUpdate`):
  - `name`: string | null
  - `icon`: string | null

**Response:** `UserWorkspacePublic`

---

## DELETE `/api/v1/user-workspaces/{workspace_id}`
**Delete Workspace**

**Path parameters:**
- `workspace_id`: uuid

**Response:** `Message`

---
