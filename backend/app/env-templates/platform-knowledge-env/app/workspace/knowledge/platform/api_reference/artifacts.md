# Artifacts — API Reference

Auto-generated from OpenAPI spec. Tag: `artifacts`

## GET `/api/v1/artifacts/capabilities`
**Get Artifacts Capabilities**

**Response:** `ArtifactsCapabilities`

---

## POST `/api/v1/artifacts`
**Create Artifact**

**Request body** (`Body_artifacts-create_artifact`):
  - `title`: string (required)
  - `description`: string | null
  - `entry_path`: string | null
  - `source_kind`: "desktop" | "cli" | "upload"
  - `source_label`: string | null
  - `user_workspace_id`: string | null
  - `archive`: string | null
  - `files`: array | null

**Response:** `ArtifactPublished`

---

## GET `/api/v1/artifacts`
**List Artifacts**

**Query parameters:**
- `scope`: "mine" | "shared_with_me", default: `mine`
- `user_workspace_id`: string | null
- `q`: string | null
- `source_key`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `ArtifactsPublic`

---

## POST `/api/v1/artifacts/{artifact_id}/revisions`
**Republish Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Request body** (`Body_artifacts-republish_artifact`):
  - `note`: string | null
  - `entry_path`: string | null
  - `title`: string | null
  - `description`: string | null
  - `archive`: string | null
  - `files`: array | null

**Response:** `ArtifactPublished`

---

## GET `/api/v1/artifacts/{artifact_id}`
**Get Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Response:** `ArtifactDetailPublic`

---

## PATCH `/api/v1/artifacts/{artifact_id}`
**Update Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Request body** (`ArtifactUpdate`):
  - `title`: string | null
  - `description`: string | null
  - `entry_path`: string | null
  - `visibility`: string | null

**Response:** `ArtifactPublic`

---

## DELETE `/api/v1/artifacts/{artifact_id}`
**Delete Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Response:** `Message`

---

## DELETE `/api/v1/artifacts/{artifact_id}/revisions/{number}`
**Delete Artifact Revision**

**Path parameters:**
- `artifact_id`: uuid
- `number`: integer

**Response:** `Message`

---

## GET `/api/v1/artifacts/{artifact_id}/download`
**Download Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Query parameters:**
- `rev`: integer | null

---

## POST `/api/v1/artifacts/{artifact_id}/view`
**View Artifact**

**Path parameters:**
- `artifact_id`: uuid

**Query parameters:**
- `rev`: integer | null

**Response:** `ArtifactViewPublic`

---

## PUT `/api/v1/artifacts/{artifact_id}/grants`
**Replace Artifact Grants**

**Path parameters:**
- `artifact_id`: uuid


---

## POST `/api/v1/artifacts/{artifact_id}/links`
**Create Artifact Link**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `artifact_id`: uuid

**Request body** (`ArtifactLinkCreate`):
  - `label`: string | null
  - `expires_at`: string | null
  - `with_code`: boolean
  - `pinned_revision_id`: string | null

**Response:** `ArtifactLinkCreated`

---

## DELETE `/api/v1/artifacts/{artifact_id}/links/{link_id}`
**Revoke Artifact Link**

**Path parameters:**
- `artifact_id`: uuid
- `link_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/artifacts/from-environment`
**Publish Artifact From Environment**

**Request body** (`ArtifactFromEnvironmentRequest`):
  - `path`: string (required)
  - `title`: string (required)
  - `description`: string | null
  - `entry`: string | null
  - `artifact_id`: string | null
  - `environment_id`: uuid (required)

**Response:** `ArtifactPublished`

---
