# Admin Provider Credentials — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-provider-credentials`

## GET `/api/v1/admin/provider-admin-credentials/`
**List Provider Admin Credentials**

---

## POST `/api/v1/admin/provider-admin-credentials/`
**Create Provider Admin Credential**

**Request body** (`ProviderAdminCredentialCreate`):
  - `name`: string (required)
  - `provider_type`: AICredentialType (required)
  - `secret`: string (required)
  - `config`: ProviderAdminCredentialConfig (required)

**Response:** `ProviderAdminCredentialPublic`

---

## GET `/api/v1/admin/provider-admin-credentials/{credential_id}`
**Get Provider Admin Credential**

**Path parameters:**
- `credential_id`: uuid

**Response:** `ProviderAdminCredentialPublic`

---

## PATCH `/api/v1/admin/provider-admin-credentials/{credential_id}`
**Update Provider Admin Credential**

**Path parameters:**
- `credential_id`: uuid

**Request body** (`ProviderAdminCredentialUpdate`):
  - `name`: string | null
  - `secret`: string | null
  - `config`: ProviderAdminCredentialConfig | null

**Response:** `ProviderAdminCredentialPublic`

---

## DELETE `/api/v1/admin/provider-admin-credentials/{credential_id}`
**Delete Provider Admin Credential**

**Path parameters:**
- `credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/admin/provider-admin-credentials/{credential_id}/verify`
**Verify Provider Admin Credential**

**Path parameters:**
- `credential_id`: uuid

**Response:** `ProviderAdminCredentialVerifyResult`

---

## POST `/api/v1/admin/provider-admin-credentials/{credential_id}/apply-spend-limit`
**Apply Provider Spend Limit**

**Path parameters:**
- `credential_id`: uuid

**Response:** `ProviderAdminCredentialVerifyResult`

---
