# Credentials — API Reference

Auto-generated from OpenAPI spec. Tag: `credentials`

## POST `/api/v1/credentials/{credential_id}/shares`
**Share Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`CredentialShareCreate`):
  - `shared_with_email`: string (required)

**Response:** `CredentialSharePublic`

---

## GET `/api/v1/credentials/{credential_id}/shares`
**Get Credential Shares**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `CredentialSharesPublic`

---

## DELETE `/api/v1/credentials/{credential_id}/shares/{share_id}`
**Revoke Credential Share**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid
- `share_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/credentials/shared-with-me`
**Get Credentials Shared With Me**

> Not available via `cinna api` (excluded_path).

**Response:** `SharedCredentialsPublic`

---

## PATCH `/api/v1/credentials/{credential_id}/sharing`
**Update Credential Sharing**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`Body_credentials-update_credential_sharing`):
  - `allow_sharing`: boolean (required)

**Response:** `CredentialPublic`

---

## GET `/api/v1/credentials/`
**Read Credentials**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `user_workspace_id`: string | null

**Response:** `CredentialsPublic`

---

## POST `/api/v1/credentials/`
**Create Credential**

> Not available via `cinna api` (excluded_path).

**Request body** (`CredentialCreate`):
  - `name`: string (required)
  - `type`: CredentialType (required)
  - `notes`: string | null
  - `allow_local_use`: boolean
  - `allow_sharing`: boolean
  - `allow_template_sharing`: boolean
  - `service_uri`: string | null
  - `mcp_mode_conversation`: boolean
  - `mcp_mode_building`: boolean
  - `mcp_consumer_agent_id`: string | null
  - `mcp_auth_mode`: string | null
  - `credential_data`: object | null
  - `user_workspace_id`: string | null
  - `template_private_fields`: array | null

**Response:** `CredentialPublic`

---

## GET `/api/v1/credentials/{id}`
**Read Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `CredentialPublic`

---

## PUT `/api/v1/credentials/{id}`
**Update Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Request body** (`CredentialUpdate`):
  - `name`: string | null
  - `notes`: string | null
  - `credential_data`: object | null
  - `allow_local_use`: boolean | null
  - `allow_sharing`: boolean | null
  - `allow_template_sharing`: boolean | null
  - `template_private_fields`: array | null
  - `service_uri`: string | null
  - `mcp_mode_conversation`: boolean | null
  - `mcp_mode_building`: boolean | null

**Response:** `CredentialPublic`

---

## DELETE `/api/v1/credentials/{id}`
**Delete Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## GET `/api/v1/credentials/{id}/with-data`
**Read Credential With Data**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `CredentialWithData`

---

## POST `/api/v1/credentials/{id}/agent-api-key/reveal`
**Reveal Agent Api Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `AgentApiKeyRevealResponse`

---

## GET `/api/v1/credentials/{id}/agent-api-key`
**Read Agent Api Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `AgentApiKeyHolderInfo`

---

## POST `/api/v1/credentials/{id}/agent-api-key/rotate`
**Rotate Agent Api Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `AgentApiKeyRotateResponse`

---

## GET `/api/v1/credentials/{id}/agent-api-key/spec`
**Read Agent Api Key Spec**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

---

## GET `/api/v1/credentials/{id}/agent-api-connection`
**Read Agent Api Connection**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `AgentApiConnectionInfo`

---

## GET `/api/v1/credentials/{id}/deletion-impact`
**Get Credential Deletion Impact**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `CredentialDeletionImpact`

---

## GET `/api/v1/credentials/{id}/bundles`
**List Credential Bundle Usages**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `id`: uuid

**Response:** `CredentialBundleUsages`

---

## POST `/api/v1/credentials/verify/odoo`
**Verify Odoo Credential**

> Not available via `cinna api` (excluded_path).

**Request body** (`OdooVerifyRequest`):
  - `url`: string (required)
  - `database_name`: string (required)
  - `login`: string (required)
  - `api_token`: string (required)

**Response:** `OdooVerifyResponse`

---

## POST `/api/v1/credentials/{credential_id}/oauth/authorize`
**Oauth Authorize**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthAuthorizeResponse`

---

## POST `/api/v1/credentials/oauth/callback`
**Oauth Callback**

> Not available via `cinna api` (excluded_path).

**Request body** (`OAuthCallbackRequest`):
  - `code`: string (required)
  - `state`: string (required)

**Response:** `OAuthCallbackResponse`

---

## GET `/api/v1/credentials/{credential_id}/oauth/metadata`
**Get Oauth Metadata**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthMetadataResponse`

---

## POST `/api/v1/credentials/{credential_id}/oauth/refresh`
**Refresh Oauth Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `OAuthRefreshResponse`

---
