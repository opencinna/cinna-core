# Ssh Keys — API Reference

Auto-generated from OpenAPI spec. Tag: `ssh-keys`

## GET `/api/v1/ssh-keys/`
**Read Ssh Keys**

**Response:** `SSHKeysPublic`

---

## POST `/api/v1/ssh-keys/`
**Import Ssh Key**

**Request body** (`SSHKeyImport`):
  - `name`: string (required)
  - `public_key`: string (required)
  - `private_key`: string (required)
  - `passphrase`: string | null

**Response:** `SSHKeyPublic`

---

## GET `/api/v1/ssh-keys/{id}`
**Read Ssh Key**

**Path parameters:**
- `id`: uuid

**Response:** `SSHKeyPublic`

---

## PUT `/api/v1/ssh-keys/{id}`
**Update Ssh Key**

**Path parameters:**
- `id`: uuid

**Request body** (`SSHKeyUpdate`):
  - `name`: string | null

**Response:** `SSHKeyPublic`

---

## DELETE `/api/v1/ssh-keys/{id}`
**Delete Ssh Key**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/ssh-keys/generate`
**Generate Ssh Key**

**Request body** (`SSHKeyGenerate`):
  - `name`: string (required)

**Response:** `SSHKeyPublic`

---
