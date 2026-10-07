# Admin Ai Keys — API Reference

Auto-generated from OpenAPI spec. Tag: `admin-ai-keys`

## GET `/api/v1/admin/ai-credentials/keys/`
**List Ai Keys**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `q`: string | null
- `status`: array | null
- `kind`: AdminAIKeyKind | null
- `provider_id`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `50`

**Response:** `AdminAIKeysPublic`

---

## DELETE `/api/v1/admin/ai-credentials/keys/{membership_id}`
**Revoke Ai Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `membership_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/admin/ai-credentials/keys/{membership_id}/rotate`
**Rotate Ai Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `membership_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/admin/ai-credentials/keys/{membership_id}/default`
**Set Ai Key As Default**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `membership_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/admin/ai-credentials/keys/{membership_id}/retry`
**Retry Ai Key**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `membership_id`: uuid

**Response:** `Message`

---
