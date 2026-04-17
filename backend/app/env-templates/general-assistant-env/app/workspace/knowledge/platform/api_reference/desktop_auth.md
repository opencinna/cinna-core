# Desktop Auth — API Reference

Auto-generated from OpenAPI spec. Tag: `desktop-auth`

## GET `/api/v1/desktop-auth/clients`
**List Desktop Clients**

---

## DELETE `/api/v1/desktop-auth/clients/{client_id}`
**Revoke Desktop Client**

**Path parameters:**
- `client_id`: string

---

## GET `/api/v1/desktop-auth/authorize`
**Authorize**

**Query parameters:**
- `redirect_uri`: string (required)
- `code_challenge`: string (required)
- `state`: string (required)
- `code_challenge_method`: string, default: `S256`
- `client_id`: string | null
- `device_name`: string | null
- `platform`: string | null
- `app_version`: string | null

---

## GET `/api/v1/desktop-auth/requests/{nonce}`
**Get Auth Request**

**Path parameters:**
- `nonce`: string

**Response:** `object`

---

## POST `/api/v1/desktop-auth/consent`
**Consent**

**Request body** (`ConsentRequest`):
  - `request_nonce`: string (required)
  - `action`: string (required)

**Response:** `ConsentResponse`

---

## POST `/api/v1/desktop-auth/token`
**Token Endpoint**

**Request body** (`TokenRequest`):
  - `grant_type`: string (required)
  - `client_id`: string (required)
  - `code`: string | null
  - `redirect_uri`: string | null
  - `code_verifier`: string | null
  - `refresh_token`: string | null

**Response:** `TokenResponse`

---

## GET `/api/v1/desktop-auth/userinfo`
**Userinfo**

**Response:** `UserInfoResponse`
  - `sub`: string (required)
  - `email`: string (required)
  - `full_name`: string | null
  - `username`: string | null

---

## POST `/api/v1/desktop-auth/revoke`
**Revoke**

**Request body** (`RevokeRequest`):
  - `client_id`: string | null
  - `refresh_token`: string | null

---

## GET `/.well-known/cinna-desktop`
**Cinna Desktop Discovery**

**Response:** `object`

---
