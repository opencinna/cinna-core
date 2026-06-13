# Admin Llm Providers — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-llm-providers`

## POST `/api/v1/admin/llm-providers/`
**Create Managed Ai Credential**

**Request body** (`ManagedAICredentialCreate`):
  - `name`: string (required)
  - `type`: AICredentialType (required)
  - `api_key`: string (required)
  - `base_url`: string | null
  - `model`: string | null
  - `expiry_notification_date`: string | null
  - `target_user_ids`: uuid[] (required)
  - `set_as_default`: boolean
  - `set_user_sdk_defaults`: boolean
  - `sdk_default_modes`: string[]

**Response:** `ManagedAICredentialReconcileResult`

---

## GET `/api/v1/admin/llm-providers/`
**List Managed Ai Credentials**

**Query parameters:**
- `managed_by_id`: string | null
- `target_user_id`: string | null

---

## GET `/api/v1/admin/llm-providers/{managed_credential_id}`
**Get Managed Ai Credential**

**Path parameters:**
- `managed_credential_id`: uuid

**Response:** `ManagedAICredentialPublic`

---

## PATCH `/api/v1/admin/llm-providers/{managed_credential_id}`
**Update Managed Ai Credential**

**Path parameters:**
- `managed_credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Request body** (`ManagedAICredentialUpdate`):
  - `name`: string | null
  - `api_key`: string | null
  - `base_url`: string | null
  - `model`: string | null
  - `expiry_notification_date`: string | null
  - `target_user_ids`: array | null
  - `set_as_default`: boolean | null
  - `set_user_sdk_defaults`: boolean | null
  - `sdk_default_modes`: array | null

**Response:** `ManagedAICredentialReconcileResult`

---

## DELETE `/api/v1/admin/llm-providers/{managed_credential_id}`
**Delete Managed Ai Credential**

**Path parameters:**
- `managed_credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/admin/llm-providers/{managed_credential_id}/set-default`
**Set Managed Ai Credential Default**

**Path parameters:**
- `managed_credential_id`: uuid

**Response:** `ManagedAICredentialPublic`

---

## POST `/api/v1/admin/llm-providers/test-connection`
**Test Managed Ai Credential Connection**

**Query parameters:**
- `managed_credential_id`: string | null

**Request body** (`AICredentialTestRequest`):
  - `type`: AICredentialType (required)
  - `api_key`: string | null
  - `base_url`: string | null
  - `credential_id`: string | null

**Response:** `AICredentialTestResult`

---
