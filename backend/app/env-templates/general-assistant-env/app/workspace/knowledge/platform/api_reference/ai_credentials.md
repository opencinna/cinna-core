# Ai Credentials — API Reference

Auto-generated from OpenAPI spec. Tag: `ai-credentials`

## GET `/api/v1/ai-credentials/`
**List Ai Credentials**

**Response:** `AICredentialsPublic`

---

## POST `/api/v1/ai-credentials/`
**Create Ai Credential**

**Request body** (`AICredentialCreate`):
  - `name`: string (required)
  - `type`: AICredentialType (required)
  - `expiry_notification_date`: string | null
  - `api_key`: string (required)
  - `base_url`: string | null
  - `model`: string | null

**Response:** `AICredentialPublic`

---

## GET `/api/v1/ai-credentials/{credential_id}`
**Get Ai Credential**

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialPublic`

---

## PATCH `/api/v1/ai-credentials/{credential_id}`
**Update Ai Credential**

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

**Path parameters:**
- `credential_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/ai-credentials/{credential_id}/set-default`
**Set Ai Credential Default**

**Path parameters:**
- `credential_id`: uuid

**Response:** `AICredentialPublic`

---

## GET `/api/v1/ai-credentials/{credential_id}/affected-environments`
**Get Affected Environments**

**Path parameters:**
- `credential_id`: uuid

**Response:** `AffectedEnvironmentsPublic`

---
