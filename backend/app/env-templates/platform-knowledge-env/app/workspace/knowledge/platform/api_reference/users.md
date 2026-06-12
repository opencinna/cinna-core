# Users — API Reference

Auto-generated from OpenAPI spec. Tag: `users`

## GET `/api/v1/users/`
**Read Users**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `role`: string | null

**Response:** `UsersPublic`

---

## POST `/api/v1/users/`
**Create User**

**Request body** (`UserCreate`):
  - `email`: string (required)
  - `is_active`: boolean
  - `is_superuser`: boolean
  - `full_name`: string | null
  - `username`: string | null
  - `role`: string
  - `password`: string (required)

**Response:** `UserPublic`

---

## GET `/api/v1/users/search`
**Search Users**

**Query parameters:**
- `q`: string (required)
- `limit`: integer, default: `10`

**Response:** `UsersSearchPublic`

---

## GET `/api/v1/users/me`
**Read User Me**

**Response:** `UserPublic`

---

## DELETE `/api/v1/users/me`
**Delete User Me**

**Response:** `Message`

---

## PATCH `/api/v1/users/me`
**Update User Me**

**Request body** (`UserUpdateMe`):
  - `full_name`: string | null
  - `email`: string | null
  - `username`: string | null
  - `default_sdk_conversation`: string | null
  - `default_sdk_building`: string | null
  - `default_ai_functions_sdk`: string | null
  - `default_ai_functions_credential_id`: string | null
  - `workspaces_enabled`: boolean | null
  - `default_ai_credential_conversation_id`: string | null
  - `default_ai_credential_building_id`: string | null
  - `default_model_override_conversation`: string | null
  - `default_model_override_building`: string | null

**Response:** `UserPublic`

---

## PATCH `/api/v1/users/me/password`
**Update Password Me**

**Request body** (`UpdatePassword`):
  - `current_password`: string (required)
  - `new_password`: string (required)

**Response:** `Message`

---

## POST `/api/v1/users/me/set-password`
**Set Password Me**

**Request body** (`SetPassword`):
  - `new_password`: string (required)

**Response:** `Message`

---

## GET `/api/v1/users/me/role`
**Read My Role**

**Response:** `UserRolePublic`

---

## PATCH `/api/v1/users/{user_id}/role`
**Update User Role**

**Path parameters:**
- `user_id`: uuid

**Request body** (`UserRoleUpdate`):
  - `role`: string (required)

**Response:** `UserPublic`

---

## POST `/api/v1/users/signup`
**Register User**

**Request body** (`UserRegister`):
  - `email`: string (required)
  - `password`: string (required)
  - `full_name`: string | null

**Response:** `UserPublic`

---

## GET `/api/v1/users/{user_id}`
**Read User By Id**

**Path parameters:**
- `user_id`: uuid

**Response:** `UserPublic`

---

## PATCH `/api/v1/users/{user_id}`
**Update User**

**Path parameters:**
- `user_id`: uuid

**Request body** (`UserUpdate`):
  - `email`: string | null
  - `is_active`: boolean
  - `is_superuser`: boolean
  - `full_name`: string | null
  - `username`: string | null
  - `role`: string
  - `password`: string | null

**Response:** `UserPublic`

---

## DELETE `/api/v1/users/{user_id}`
**Delete User**

**Path parameters:**
- `user_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/users/me/ai-credentials/status`
**Get Ai Credentials Status**

**Response:** `UserPublicWithAICredentials`

---

## GET `/api/v1/users/me/ai-credentials`
**Get Ai Credentials**

**Response:** `AIServiceCredentials`

---

## DELETE `/api/v1/users/me/ai-credentials`
**Delete Ai Credentials**

**Response:** `Message`

---

## PATCH `/api/v1/users/me/ai-credentials`
**Update Ai Credentials**

**Request body** (`AIServiceCredentialsUpdate`):
  - `anthropic_api_key`: string | null
  - `openai_api_key`: string | null
  - `google_ai_api_key`: string | null
  - `minimax_api_key`: string | null
  - `openai_compatible_api_key`: string | null
  - `openai_compatible_base_url`: string | null
  - `openai_compatible_model`: string | null

**Response:** `Message`

---
