# App Data — API Reference

Auto-generated from OpenAPI spec. Tag: `app-data`

## GET `/api/v1/users/me/app-data`
**List App Data Volumes**

**Response:** `AppDataVolumesPublic`

---

## POST `/api/v1/users/me/app-data/{volume_id}/recompute-size`
**Recompute App Data Size**

**Path parameters:**
- `volume_id`: uuid

**Response:** `AppDataVolumePublic`

---

## DELETE `/api/v1/users/me/app-data/{volume_id}`
**Wipe App Data Volume**

**Path parameters:**
- `volume_id`: uuid

---
