# Server Config — API Reference

Auto-generated from OpenAPI spec. Tag: `server-config`

## GET `/api/v1/server-config/access-policy`
**Get Access Policy**

**Response:** `AccessPolicyPublic`

---

## GET `/api/v1/server-config/disclaimer`
**Get Disclaimer**

**Response:** `DisclaimerPublic`

---

## GET `/api/v1/admin/server-config`
**Get Server Config**

**Response:** `ServerConfig`

---

## PUT `/api/v1/admin/server-config`
**Update Server Config**

**Request body** (`ServerConfigUpdate`):
  - `disclaimer_enabled`: boolean | null
  - `disclaimer_markdown`: string | null
  - `disclaimer_display_mode`: string | null
  - `local_agent_kit_enabled`: boolean | null
  - `registration_mode`: string | null
  - `allowed_email_patterns`: string | null
  - `password_auth_enabled`: boolean | null
  - `google_auto_register`: boolean | null
  - `default_user_role`: string | null
  - `invite_include_desktop_default`: boolean | null

**Response:** `ServerConfig`

---
