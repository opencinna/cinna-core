# Bundles — API Reference

Auto-generated from OpenAPI spec. Tag: `bundles`

## GET `/api/v1/bundles/`
**List Bundles**

**Response:** `AgentBundlesPublic`

---

## GET `/api/v1/bundles/{bundle_uuid}`
**Get Bundle**

**Path parameters:**
- `bundle_uuid`: uuid

**Response:** `AgentBundlePublic`

---

## PATCH `/api/v1/bundles/{bundle_uuid}`
**Update Bundle**

**Path parameters:**
- `bundle_uuid`: uuid

**Request body** (`AgentBundleUpdate`):
  - `display_name`: string | null
  - `description`: string | null
  - `is_listed`: boolean | null
  - `visibility`: string | null
  - `default_install_mode`: string | null

**Response:** `AgentBundlePublic`

---

## DELETE `/api/v1/bundles/{bundle_uuid}`
**Delete Bundle**

**Path parameters:**
- `bundle_uuid`: uuid

**Response:** `object`

---

## GET `/api/v1/bundles/{bundle_uuid}/revisions`
**List Revisions**

**Path parameters:**
- `bundle_uuid`: uuid

**Response:** `AgentBundleRevisionsPublic`

---

## DELETE `/api/v1/bundles/{bundle_uuid}/revisions/{revision_id}`
**Delete Revision**

**Path parameters:**
- `bundle_uuid`: uuid
- `revision_id`: uuid

**Response:** `object`

---

## GET `/api/v1/bundles/{bundle_uuid}/grants`
**List Grants**

**Path parameters:**
- `bundle_uuid`: uuid

**Response:** `BundleAccessGrantsPublic`

---

## POST `/api/v1/bundles/{bundle_uuid}/grants`
**Add Grant**

**Path parameters:**
- `bundle_uuid`: uuid

**Request body** (`BundleAccessGrantCreate`):
  - `email`: string (required)

**Response:** `BundleAccessGrantPublic`

---

## DELETE `/api/v1/bundles/{bundle_uuid}/grants/{grant_id}`
**Revoke Grant**

**Path parameters:**
- `bundle_uuid`: uuid
- `grant_id`: uuid

**Response:** `object`

---
