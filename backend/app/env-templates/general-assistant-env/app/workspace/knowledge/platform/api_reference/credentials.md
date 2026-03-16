# Credentials — API Reference

Auto-generated from OpenAPI spec. Tag: `credentials`

## POST `/api/v1/credentials/{credential_id}/shares`
**Share Credential**

**Path parameters:**
- `credential_id`: uuid

**Request body** (`CredentialShareCreate`):
  - `shared_with_email`: string (required)

**Response:** `CredentialSharePublic`

---

## GET `/api/v1/credentials/{credential_id}/shares`
**Get Credential Shares**

**Path parameters:**
- `credential_id`: uuid

**Response:** `CredentialSharesPublic`

---

## DELETE `/api/v1/credentials/{credential_id}/shares/{share_id}`
**Revoke Credential Share**

**Path parameters:**
- `credential_id`: uuid
- `share_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/credentials/shared-with-me`
**Get Credentials Shared With Me**

**Response:** `SharedCredentialsPublic`

---

## PATCH `/api/v1/credentials/{credential_id}/sharing`
**Update Credential Sharing**

**Path parameters:**
- `credential_id`: uuid

**Request body** (`Body_credentials-update_credential_sharing`):
  - `allow_sharing`: boolean (required)

**Response:** `CredentialPublic`

---

## GET `/api/v1/credentials/`
**Read Credentials**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `user_workspace_id`: string | null

**Response:** `CredentialsPublic`

---

## POST `/api/v1/credentials/`
**Create Credential**

**Request body** (`CredentialCreate`):
  - `name`: string (required)
  - `type`: CredentialType (required)
  - `notes`: string | null
  - `allow_sharing`: boolean
  - `credential_data`: object | null
  - `user_workspace_id`: string | null

**Response:** `CredentialPublic`

---

## GET `/api/v1/credentials/{id}`
**Read Credential**

**Path parameters:**
- `id`: uuid

**Response:** `CredentialPublic`

---

## PUT `/api/v1/credentials/{id}`
**Update Credential**

**Path parameters:**
- `id`: uuid

**Request body** (`CredentialUpdate`):
  - `name`: string | null
  - `notes`: string | null
  - `credential_data`: object | null
  - `allow_sharing`: boolean | null

**Response:** `CredentialPublic`

---

## DELETE `/api/v1/credentials/{id}`
**Delete Credential**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## GET `/api/v1/credentials/{id}/with-data`
**Read Credential With Data**

**Path parameters:**
- `id`: uuid

**Response:** `CredentialWithData`

---

## POST `/api/v1/credentials/verify/odoo`
**Verify Odoo Credential**

**Request body** (`OdooVerifyRequest`):
  - `url`: string (required)
  - `database_name`: string (required)
  - `login`: string (required)
  - `api_token`: string (required)

**Response:** `OdooVerifyResponse`

---

## POST `/api/v1/credentials/{credential_id}/oauth/authorize`
**Oauth Authorize**

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthAuthorizeResponse`

---

## POST `/api/v1/credentials/oauth/callback`
**Oauth Callback**

**Request body** (`OAuthCallbackRequest`):
  - `code`: string (required)
  - `state`: string (required)

**Response:** `OAuthCallbackResponse`

---

## GET `/api/v1/credentials/{credential_id}/oauth/metadata`
**Get Oauth Metadata**

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthMetadataResponse`

---

## POST `/api/v1/credentials/{credential_id}/oauth/refresh`
**Refresh Oauth Token**

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthRefreshResponse`

---
