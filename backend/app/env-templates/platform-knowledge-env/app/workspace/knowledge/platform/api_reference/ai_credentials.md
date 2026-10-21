# Ai Credentials — API Reference

Auto-generated from OpenAPI spec. Tag: `ai-credentials`

## GET `/api/v1/ai-credentials/`
**List Ai Credentials**

> Not available via `cinna api` (excluded_path).

**Response:** `AICredentialsPublic`

---

## POST `/api/v1/ai-credentials/`
**Create Ai Credential**

> Not available via `cinna api` (excluded_path).

**Request body** (`AICredentialCreate`):
  - `name`: string (required)
  - `type`: AICredentialType (required)
  - `expiry_notification_date`: string | null
  - `api_key`: string (required)
  - `base_url`: string | null
  - `model`: string | null

**Response:** `AICredentialPublic`

---

## GET `/api/v1/ai-credentials/provisioning`
**List My Key Provisionings**

> Not available via `cinna api` (excluded_path).

---

## GET `/api/v1/ai-credentials/shared-with-me`
**List Ai Credentials Shared With Me**

> Not available via `cinna api` (excluded_path).

**Response:** `SharedAICredentialsPublic`

---

## GET `/api/v1/ai-credentials/resolve-default/{sdk_engine}`
**Resolve Default Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `sdk_engine`: string

---

## POST `/api/v1/ai-credentials/test-connection`
**Test Ai Credential Connection**

> Not available via `cinna api` (excluded_path).

**Request body** (`AICredentialTestRequest`):
  - `type`: AICredentialType (required)
  - `api_key`: string | null
  - `base_url`: string | null
  - `credential_id`: string | null

**Response:** `AICredentialTestResult`

---

## GET `/api/v1/ai-credentials/{credential_id}`
**Get Ai Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialPublic`

---

## PATCH `/api/v1/ai-credentials/{credential_id}`
**Update Ai Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AICredentialUpdate`):
  - `name`: string | null
  - `api_key`: string | null
  - `base_url`: string | null
  - `model`: string | null
  - `expiry_notification_date`: string | null

**Response:** `AICredentialPublic`

---

## DELETE `/api/v1/ai-credentials/{credential_id}`
**Delete Ai Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## GET `/api/v1/ai-credentials/{credential_id}/deletion-impact`
**Get Ai Credential Deletion Impact**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialDeletionImpact`

---

## POST `/api/v1/ai-credentials/{credential_id}/set-default`
**Set Ai Credential Default**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialPublic`

---

## GET `/api/v1/ai-credentials/{credential_id}/affected-environments`
**Get Affected Environments**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `AffectedEnvironmentsPublic`

---

## POST `/api/v1/ai-credentials/{credential_id}/group-shares`
**Share Ai Credential With Group**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AICredentialGroupShareCreate`):
  - `group_id`: uuid (required)

**Response:** `AICredentialGroupSharePublic`

---

## GET `/api/v1/ai-credentials/{credential_id}/group-shares`
**List Ai Credential Group Shares**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialGroupSharesPublic`

---

## DELETE `/api/v1/ai-credentials/{credential_id}/group-shares/{group_id}`
**Revoke Ai Credential Group Share**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid
- `group_id`: uuid

**Response:** `Message`

---
