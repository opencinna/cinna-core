# Mfa — API Reference

Auto-generated from OpenAPI spec. Tag: `mfa`

## GET `/api/v1/users/me/mfa/status`
**Mfa Status**

**Response:** `MfaStatus`

---

## GET `/api/v1/users/me/mfa/passkeys`
**List Passkeys**

**Response:** `UserPasskeysPublic`

---

## POST `/api/v1/users/me/mfa/passkeys/begin`
**Begin Passkey Registration**


**Response:** `BeginPasskeyRegistrationResponse`

---

## POST `/api/v1/users/me/mfa/passkeys/finish`
**Finish Passkey Registration**

**Request body** (`PasskeyFinishRequest`):
  - `challenge_token`: string (required)
  - `credential`: object (required)
  - `nickname`: string (required)

**Response:** `object`

---

## PATCH `/api/v1/users/me/mfa/passkeys/{passkey_id}`
**Rename Passkey**

**Path parameters:**
- `passkey_id`: uuid

**Request body** (`UserPasskeyUpdate`):
  - `nickname`: string (required)

**Response:** `UserPasskeyPublic`

---

## DELETE `/api/v1/users/me/mfa/passkeys/{passkey_id}`
**Delete Passkey**

**Path parameters:**
- `passkey_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/users/me/mfa/totp/begin`
**Begin Totp Enrollment**

**Response:** `TotpEnrollResponse`

---

## POST `/api/v1/users/me/mfa/totp/finish`
**Finish Totp Enrollment**

**Request body** (`TotpFinishRequest`):
  - `secret_token`: string (required)
  - `code`: string (required)

**Response:** `object`

---

## DELETE `/api/v1/users/me/mfa/totp`
**Disable Totp**

**Request body** (`StepUpProof`):
  - `password`: string | null
  - `totp_code`: string | null
  - `passkey_assertion`: object | null
  - `passkey_challenge_token`: string | null

**Response:** `Message`

---

## GET `/api/v1/users/me/mfa/recovery-codes`
**Recovery Codes Status**

**Response:** `RecoveryCodeStatus`

---

## POST `/api/v1/users/me/mfa/recovery-codes/regenerate`
**Regenerate Recovery Codes**

**Request body** (`StepUpProof`):
  - `password`: string | null
  - `totp_code`: string | null
  - `passkey_assertion`: object | null
  - `passkey_challenge_token`: string | null

**Response:** `RecoveryCodesPlaintext`

---

## POST `/api/v1/users/me/mfa/step-up/passkey/options`
**Begin Step Up Passkey**

**Response:** `StepUpPasskeyOptions`

---

## POST `/api/v1/users/me/mfa/disable`
**Disable Two Factor**

**Request body** (`StepUpProof`):
  - `password`: string | null
  - `totp_code`: string | null
  - `passkey_assertion`: object | null
  - `passkey_challenge_token`: string | null

**Response:** `Message`

---
