# Identity Contacts — API Reference

Auto-generated from OpenAPI spec. Tag: `identity-contacts`

## GET `/api/v1/users/me/identity-contacts/`
**List Identity Contacts**

---

## PATCH `/api/v1/users/me/identity-contacts/{owner_id}`
**Toggle Identity Contact**

**Path parameters:**
- `owner_id`: uuid

**Request body** (`ToggleIdentityContactRequest`):
  - `is_enabled`: boolean (required)

**Response:** `Message`

---
