# Mail Servers — API Reference

Auto-generated from OpenAPI spec. Tag: `mail-servers`

## GET `/api/v1/mail-servers/`
**List Mail Servers**

**Query parameters:**
- `server_type`: MailServerType | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `MailServerConfigsPublic`

---

## POST `/api/v1/mail-servers/`
**Create Mail Server**

**Request body** (`MailServerConfigCreate`):
  - `name`: string (required)
  - `server_type`: MailServerType (required)
  - `host`: string (required)
  - `port`: integer (required)
  - `encryption_type`: EncryptionType
  - `username`: string (required)
  - `password`: string (required)

**Response:** `MailServerConfigPublic`

---

## GET `/api/v1/mail-servers/{server_id}`
**Get Mail Server**

**Path parameters:**
- `server_id`: uuid

**Response:** `MailServerConfigPublic`

---

## PUT `/api/v1/mail-servers/{server_id}`
**Update Mail Server**

**Path parameters:**
- `server_id`: uuid

**Request body** (`MailServerConfigUpdate`):
  - `name`: string | null
  - `host`: string | null
  - `port`: integer | null
  - `encryption_type`: EncryptionType | null
  - `username`: string | null
  - `password`: string | null

**Response:** `MailServerConfigPublic`

---

## DELETE `/api/v1/mail-servers/{server_id}`
**Delete Mail Server**

**Path parameters:**
- `server_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/mail-servers/{server_id}/test-connection`
**Test Mail Server Connection**

**Path parameters:**
- `server_id`: uuid

**Response:** `Message`

---
