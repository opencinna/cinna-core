# Invitations — API Reference

Auto-generated from OpenAPI spec. Tag: `invitations`

## POST `/api/v1/invitations/lookup`
**Lookup Invitation**

**Request body** (`InvitationLookupRequest`):
  - `token`: string (required)

**Response:** `InvitationLookupPublic`

---

## POST `/api/v1/invitations/accept`
**Accept Invitation**

**Request body** (`AcceptInvitationRequest`):
  - `token`: string (required)
  - `password`: string (required)
  - `full_name`: string | null

---
