# App Sync — API Reference

Auto-generated from OpenAPI spec. Tag: `App Sync`

## POST `/api/v1/app-sync/`
**Sync**

**Request body** (`SyncRequest`):
  - `cursor`: integer
  - `changes`: SyncRecordUpsert[]
  - `collections`: array | null
  - `limit`: integer

**Response:** `SyncResponse`

---

## DELETE `/api/v1/app-sync/`
**Wipe**


**Response:** `Message`

---

## POST `/api/v1/app-sync/pull`
**Pull**

**Request body** (`PullRequest`):
  - `cursor`: integer
  - `collections`: array | null
  - `limit`: integer

**Response:** `SyncResponse`

---

## POST `/api/v1/app-sync/push`
**Push**

**Request body** (`PushRequest`):
  - `changes`: SyncRecordUpsert[]

**Response:** `SyncResponse`

---

## GET `/api/v1/app-sync/state`
**Get State**

**Response:** `SyncStatePublic`

---

## GET `/api/v1/app-sync/encryption`
**Get Encryption**

**Response:** `EncryptionStatePublic`

---

## DELETE `/api/v1/app-sync/encryption`
**Reset Encryption**

**Response:** `EncryptionStatePublic`

---

## POST `/api/v1/app-sync/encryption/init`
**Init Encryption**

**Request body** (`EncryptionInitRequest`):
  - `device`: DeviceInput (required)
  - `envelopes`: KeyEnvelopeInput[]

**Response:** `EncryptionStatePublic`

---

## GET `/api/v1/app-sync/keys`
**List Keys**

**Query parameters:**
- `umk_version`: integer | null

---

## POST `/api/v1/app-sync/keys`
**Add Key**

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

**Path parameters:**
- `envelope_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/app-sync/devices`
**List Devices**

---

## POST `/api/v1/app-sync/devices`
**Register Device**

**Request body** (`DeviceInput`):
  - `device_label`: string (required)
  - `public_key`: string (required)
  - `external_client_id`: string | null

**Response:** `AppSyncDevicePublic`

---

## DELETE `/api/v1/app-sync/devices/{device_id}`
**Revoke Device**

**Path parameters:**
- `device_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/app-sync/pairing/start`
**Pairing Start**

**Request body** (`PairingStartRequest`):
  - `new_device_pubkey`: string (required)
  - `commitment`: string (required)
  - `device_label`: string | null

**Response:** `PairingStartResponse`

---

## GET `/api/v1/app-sync/pairing/inbox`
**Pairing Inbox**

---

## GET `/api/v1/app-sync/pairing/inbox/{pairing_id}`
**Pairing Inbox Get**

**Path parameters:**
- `pairing_id`: uuid

**Response:** `PairingInboxDetail`

---

## POST `/api/v1/app-sync/pairing/inbox/{pairing_id}/sealer-nonce`
**Pairing Set Sealer Nonce**

**Path parameters:**
- `pairing_id`: uuid

**Request body** (`PairingSealerNonceRequest`):
  - `sealer_nonce`: string (required)

**Response:** `Message`

---

## POST `/api/v1/app-sync/pairing/inbox/{pairing_id}/complete`
**Pairing Complete By Id**

**Path parameters:**
- `pairing_id`: uuid

**Request body** (`PairingCompleteRequest`):
  - `sealed_umk`: string (required)

**Response:** `Message`

---

## GET `/api/v1/app-sync/pairing/{code}`
**Pairing Get**

**Path parameters:**
- `code`: string

**Response:** `PairingStatusPublic`

---

## POST `/api/v1/app-sync/pairing/{code}/reveal`
**Pairing Reveal**

**Path parameters:**
- `code`: string

**Request body** (`PairingRevealRequest`):
  - `joiner_nonce`: string (required)

**Response:** `Message`

---
