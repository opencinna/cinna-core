# Catalog — API Reference

Auto-generated from OpenAPI spec. Tag: `catalog`

## GET `/api/v1/catalog/`
**List Catalog**

**Response:** `CatalogPublic`

---

## GET `/api/v1/catalog/{bundle_id}`
**Get Catalog Entry**

**Path parameters:**
- `bundle_id`: string

**Response:** `CatalogEntryPublic`

---

## GET `/api/v1/catalog/{bundle_id}/install-context`
**Get Install Context**

**Path parameters:**
- `bundle_id`: string

**Response:** `CatalogInstallContext`

---

## POST `/api/v1/catalog/{bundle_id}/install`
**Install Bundle**

**Path parameters:**
- `bundle_id`: string


**Response:** `AgentPublic`

---

## POST `/api/v1/catalog/{bundle_id}/admin-install`
**Admin Install Bundle**

**Path parameters:**
- `bundle_id`: string

**Request body** (`AdminInstallRequest`):
  - `credentials`: object | null
  - `ai_credential_selections`: AICredentialSelections | null
  - `target_user_id`: uuid (required)

**Response:** `AgentPublic`

---
