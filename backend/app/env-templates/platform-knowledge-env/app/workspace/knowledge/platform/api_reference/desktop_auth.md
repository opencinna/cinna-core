# Desktop Auth — API Reference

Auto-generated from OpenAPI spec. Tag: `desktop-auth`

## GET `/api/v1/desktop-auth/clients`
**List Desktop Clients**

> Not available via `cinna api` (excluded_path).

---

## DELETE `/api/v1/desktop-auth/clients/{client_id}`
**Revoke Desktop Client**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `client_id`: string

---

## GET `/api/v1/desktop-auth/authorize`
**Authorize**

> Not available via `cinna api` (excluded_path).

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

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `nonce`: string

**Response:** `object`

---

## POST `/api/v1/desktop-auth/consent`
**Consent**

> Not available via `cinna api` (excluded_path).

**Request body** (`ConsentRequest`):
  - `request_nonce`: string (required)
  - `action`: string (required)

**Response:** `ConsentResponse`

---

## POST `/api/v1/desktop-auth/token`
**Token Endpoint**

> Not available via `cinna api` (excluded_path).

**Response:** `TokenResponse`

---

## GET `/api/v1/desktop-auth/userinfo`
**Userinfo**

> Not available via `cinna api` (excluded_path).

**Response:** `UserInfoResponse`

---

## POST `/api/v1/desktop-auth/revoke`
**Revoke**

> Not available via `cinna api` (excluded_path).

**Request body** (`RevokeRequest`):
  - `client_id`: string | null
  - `refresh_token`: string | null

---

## GET `/.well-known/cinna-desktop`
**Cinna Desktop Discovery**

**Response:** `object`

---
