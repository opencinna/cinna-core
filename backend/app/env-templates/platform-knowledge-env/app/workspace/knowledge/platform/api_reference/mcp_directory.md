# Mcp Directory — API Reference

Auto-generated from OpenAPI spec. Tag: `mcp-directory`

## GET `/api/v1/mcp-directory/entries`
**List Mcp Directory Entries**

**Response:** `McpDirectoryEntriesPublic`

---

## POST `/api/v1/mcp-directory/entries`
**Create Mcp Directory Entry**

**Request body** (`McpDirectoryEntryCreate`):
  - `name`: string (required)
  - `title`: string (required)
  - `description`: string | null
  - `icon_url`: string | null
  - `categories`: string[]
  - `transport`: "stdio" | "http" | "sse" (required)
  - `url`: string | null
  - `package`: McpPackage | null
  - `auth`: "none" | "oauth" | "bearer" | "env"
  - `env`: McpEnvVar[]
  - `homepage`: string | null
  - `publisher_name`: string | null
  - `verified`: boolean

**Response:** `McpDirectoryEntryPublic`

---

## PUT `/api/v1/mcp-directory/entries/{entry_id}`
**Update Mcp Directory Entry**

**Path parameters:**
- `entry_id`: uuid

**Request body** (`McpDirectoryEntryUpdate`):
  - `title`: string | null
  - `description`: string | null
  - `icon_url`: string | null
  - `categories`: array | null
  - `transport`: string | null
  - `url`: string | null
  - `package`: McpPackage | null
  - `auth`: string | null
  - `env`: array | null
  - `homepage`: string | null
  - `publisher_name`: string | null
  - `verified`: boolean | null

**Response:** `McpDirectoryEntryPublic`

---

## DELETE `/api/v1/mcp-directory/entries/{entry_id}`
**Delete Mcp Directory Entry**

**Path parameters:**
- `entry_id`: uuid

**Response:** `Message`

---

## PUT `/api/v1/mcp-directory/entries/{entry_id}/access`
**Set Mcp Directory Entry Access**

**Path parameters:**
- `entry_id`: uuid

**Request body** (`McpDirectoryAccessUpdate`):
  - `access`: "default" | "banned" (required)
  - `reason`: string | null

**Response:** `McpDirectoryEntryPublic`

---

## POST `/api/v1/mcp-directory/import`
**Import Mcp Directory Index**

**Response:** `McpDirectoryImportResult`

---
