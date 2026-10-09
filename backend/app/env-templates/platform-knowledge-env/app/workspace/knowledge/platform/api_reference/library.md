# Library — API Reference

Auto-generated from OpenAPI spec. Tag: `library`

## GET `/api/v1/library`
**List Library**

**Response:** `LibraryListing`

---

## POST `/api/v1/library/agents`
**Create Library Agent**

**Request body** (`LibraryAgentCreate`):
  - `slug`: string (required)
  - `name`: string (required)
  - `description`: string

**Response:** `LibraryAgent`

---

## GET `/api/v1/library/admin/agents`
**List Library Admin Agents**

---

## GET `/api/v1/library/agents/{repo_id}`
**Get Library Agent**

**Path parameters:**
- `repo_id`: uuid

**Response:** `LibraryAgent`

---

## DELETE `/api/v1/library/agents/{repo_id}`
**Delete Library Agent**

**Path parameters:**
- `repo_id`: uuid

---

## GET `/api/v1/library/agents/{repo_id}/grants`
**Get Library Agent Grants**

**Path parameters:**
- `repo_id`: uuid

---

## PUT `/api/v1/library/agents/{repo_id}/grants`
**Put Library Agent Grants**

**Path parameters:**
- `repo_id`: uuid


---
