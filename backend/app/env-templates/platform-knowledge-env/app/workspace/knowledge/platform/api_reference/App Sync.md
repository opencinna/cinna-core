# App Sync — API Reference

Auto-generated from OpenAPI spec. Tag: `App Sync`

## POST `/api/v1/app-sync/`
**Sync**

> Not available via `cinna api` (excluded_path).

**Request body** (`SyncRequest`):
  - `cursor`: integer
  - `changes`: SyncRecordUpsert[]
  - `collections`: array | null
  - `limit`: integer

**Response:** `SyncResponse`

---

## DELETE `/api/v1/app-sync/`
**Wipe**

> Not available via `cinna api` (excluded_path).


**Response:** `Message`

---

## POST `/api/v1/app-sync/pull`
**Pull**

> Not available via `cinna api` (excluded_path).

**Request body** (`PullRequest`):
  - `cursor`: integer
  - `collections`: array | null
  - `limit`: integer

**Response:** `SyncResponse`

---

## POST `/api/v1/app-sync/push`
**Push**

> Not available via `cinna api` (excluded_path).

**Request body** (`PushRequest`):
  - `changes`: SyncRecordUpsert[]

**Response:** `SyncResponse`

---

## GET `/api/v1/app-sync/state`
**Get State**

> Not available via `cinna api` (excluded_path).

**Response:** `SyncStatePublic`

---

## GET `/api/v1/app-sync/encryption`
**Get Encryption**

> Not available via `cinna api` (excluded_path).

**Response:** `EncryptionStatePublic`

---

## DELETE `/api/v1/app-sync/encryption`
**Reset Encryption**

> Not available via `cinna api` (excluded_path).

**Response:** `EncryptionStatePublic`

---

## POST `/api/v1/app-sync/encryption/init`
**Init Encryption**

> Not available via `cinna api` (excluded_path).

**Request body** (`EncryptionInitRequest`):
  - `device`: DeviceInput (required)
  - `envelopes`: KeyEnvelopeInput[]

**Response:** `EncryptionStatePublic`

---

## GET `/api/v1/app-sync/keys`
**List Keys**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `umk_version`: integer | null

---

## POST `/api/v1/app-sync/keys`
**Add Key**

> Not available via `cinna api` (excluded_path).

**Request body** (`KeyEnvelopeInput`):
  - `wrap_method`: "device" | "recovery" | "passphrase" (required)
  - `umk_version`: integer
  - `wrapped_key`: string (required)
  - `kdf`: string | null
  - `kdf_params`: object | null
  - `device_id`: string | null

**Response:** `AppSyncKeyEnvelopePublic`

---

## DELETE `/api/v1/app-sync/keys/{envelope_id}`
**Delete Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `envelope_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/app-sync/devices`
**List Devices**

> Not available via `cinna api` (excluded_path).

---

## POST `/api/v1/app-sync/devices`
**Register Device**

> Not available via `cinna api` (excluded_path).

**Request body** (`DeviceInput`):
  - `device_label`: string (required)
  - `public_key`: string (required)
  - `external_client_id`: string | null

**Response:** `AppSyncDevicePublic`

---

## DELETE `/api/v1/app-sync/devices/{device_id}`
**Revoke Device**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `device_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/app-sync/pairing/start`
**Pairing Start**

> Not available via `cinna api` (excluded_path).

**Request body** (`PairingStartRequest`):
  - `new_device_pubkey`: string (required)
  - `commitment`: string (required)
  - `device_label`: string | null

**Response:** `PairingStartResponse`

---

## GET `/api/v1/app-sync/pairing/inbox`
**Pairing Inbox**

> Not available via `cinna api` (excluded_path).

---

## GET `/api/v1/app-sync/pairing/inbox/{pairing_id}`
**Pairing Inbox Get**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `pairing_id`: uuid

**Response:** `PairingInboxDetail`

---

## POST `/api/v1/app-sync/pairing/inbox/{pairing_id}/sealer-nonce`
**Pairing Set Sealer Nonce**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `pairing_id`: uuid

**Request body** (`PairingSealerNonceRequest`):
  - `sealer_nonce`: string (required)

**Response:** `Message`

---

## POST `/api/v1/app-sync/pairing/inbox/{pairing_id}/complete`
**Pairing Complete By Id**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `pairing_id`: uuid

**Request body** (`PairingCompleteRequest`):
  - `sealed_umk`: string (required)

**Response:** `Message`

---

## GET `/api/v1/app-sync/pairing/{code}`
**Pairing Get**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `code`: string

**Response:** `PairingStatusPublic`

---

## POST `/api/v1/app-sync/pairing/{code}/reveal`
**Pairing Reveal**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `code`: string

**Request body** (`PairingRevealRequest`):
  - `joiner_nonce`: string (required)

**Response:** `Message`

---
