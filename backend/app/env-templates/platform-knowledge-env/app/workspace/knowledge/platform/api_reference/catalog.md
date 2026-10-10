# Catalog — API Reference

Auto-generated from OpenAPI spec. Tag: `catalog`

## GET `/api/v1/catalog/items`
**List Catalog Items**

**Query parameters:**
- `kind`: string | null
- `runs_on`: string | null
- `scope`: string | null

**Response:** `CatalogItemsPublic`

---

## POST `/api/v1/catalog/desktop-agents`
**Create Desktop Agent**

**Request body** (`DesktopAgentCreate`):
  - `slug`: string (required)
  - `display_name`: string (required)
  - `description`: string
  - `manifest_id`: uuid (required)
  - `visibility`: "private" | "users" | "public"
  - `grants`: CatalogGrantIn[]

**Response:** `CatalogItem`

---

## GET `/api/v1/catalog/items/{item_uuid}`
**Get Catalog Item**

**Path parameters:**
- `item_uuid`: uuid

**Response:** `CatalogItemDetail`

---

## PATCH `/api/v1/catalog/items/{item_uuid}`
**Update Catalog Item**

**Path parameters:**
- `item_uuid`: uuid

**Request body** (`CatalogItemUpdate`):
  - `display_name`: string | null
  - `description`: string | null
  - `visibility`: string | null
  - `is_listed`: boolean | null

**Response:** `CatalogItem`

---

## DELETE `/api/v1/catalog/items/{item_uuid}`
**Delete Catalog Item**

**Path parameters:**
- `item_uuid`: uuid

---

## GET `/api/v1/catalog/items/{item_uuid}/grants`
**List Catalog Item Grants**

**Path parameters:**
- `item_uuid`: uuid

---

## PUT `/api/v1/catalog/items/{item_uuid}/grants`
**Put Catalog Item Grants**

**Path parameters:**
- `item_uuid`: uuid


---

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
