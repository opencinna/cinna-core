# Admin Ai Providers — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-ai-providers`

## GET `/api/v1/admin/ai-providers/adapters`
**List Provider Adapters**

**Response:** `ProviderAdaptersPublic`

---

## GET `/api/v1/admin/ai-providers/`
**List Ai Providers**

---

## POST `/api/v1/admin/ai-providers/`
**Create Ai Provider**

**Request body** (`AIProviderCreate`):
  - `name`: string (required)
  - `kind`: AIProviderKind (required)
  - `type`: AICredentialType (required)
  - `secret`: string (required)
  - `config`: AIProviderConfigInput
  - `base_url`: string | null
  - `model`: string | null
  - `auto_provision_roles`: string[]
  - `set_as_default`: boolean
  - `set_user_sdk_defaults`: boolean
  - `sdk_default_modes`: string[]
  - `default_model`: string | null
  - `available_models`: array | null
  - `model_override_conversation`: string | null
  - `model_override_building`: string | null
  - `expiry_notification_date`: string | null
  - `target_user_ids`: uuid[]

**Response:** `AIProviderPublic`

---

## GET `/api/v1/admin/ai-providers/{provider_id}`
**Get Ai Provider**

**Path parameters:**
- `provider_id`: uuid

**Response:** `AIProviderPublic`

---

## PATCH `/api/v1/admin/ai-providers/{provider_id}`
**Update Ai Provider**

**Path parameters:**
- `provider_id`: uuid

**Request body** (`AIProviderUpdate`):
  - `name`: string | null
  - `config`: AIProviderConfigInput | null
  - `base_url`: string | null
  - `model`: string | null
  - `auto_provision_roles`: array | null
  - `set_as_default`: boolean | null
  - `set_user_sdk_defaults`: boolean | null
  - `sdk_default_modes`: array | null
  - `default_model`: string | null
  - `available_models`: array | null
  - `model_override_conversation`: string | null
  - `model_override_building`: string | null
  - `expiry_notification_date`: string | null

**Response:** `AIProviderPublic`

---

## DELETE `/api/v1/admin/ai-providers/{provider_id}`
**Delete Ai Provider**

**Path parameters:**
- `provider_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/admin/ai-providers/{provider_id}/verify`
**Verify Ai Provider**

**Path parameters:**
- `provider_id`: uuid

**Response:** `AIProviderVerifyResult`

---

## POST `/api/v1/admin/ai-providers/{provider_id}/rotate-key`
**Rotate Ai Provider Key**

**Path parameters:**
- `provider_id`: uuid

**Request body** (`AIProviderRotateKey`):
  - `api_key`: string (required)

**Response:** `AIProviderPublic`

---

## POST `/api/v1/admin/ai-providers/{provider_id}/apply-to-existing`
**Apply Ai Provider To Existing**

**Path parameters:**
- `provider_id`: uuid

**Query parameters:**
- `dry_run`: boolean, default: `False`

**Response:** `ManagedAICredentialApplyResult`

---
