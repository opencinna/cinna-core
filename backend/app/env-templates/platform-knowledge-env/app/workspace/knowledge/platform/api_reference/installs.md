# Installs — API Reference

Auto-generated from OpenAPI spec. Tag: `installs`

## POST `/api/v1/agents/{agent_id}/publish`
**Publish Agent**

**Path parameters:**
- `agent_id`: uuid


**Response:** `AgentBundleRevisionPublic`

---

## POST `/api/v1/agents/{agent_id}/uninstall`
**Uninstall Install**

**Path parameters:**
- `agent_id`: uuid

**Response:** `object`

---

## POST `/api/v1/agents/{agent_id}/apply-update`
**Apply Update**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentPublic`

---

## POST `/api/v1/agents/{agent_id}/check-updates`
**Check Updates**

**Path parameters:**
- `agent_id`: uuid

**Response:** `CheckUpdatesResponse`

---

## PATCH `/api/v1/agents/{agent_id}/update-mode`
**Set Update Mode**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`SetUpdateModeRequest`):
  - `update_mode`: string (required)

**Response:** `AgentPublic`

---

## PATCH `/api/v1/agents/{agent_id}/bundle-id`
**Edit Bundle Id**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`EditBundleIdRequest`):
  - `bundle_id`: string (required)

**Response:** `AgentPublic`

---

## PATCH `/api/v1/agents/{agent_id}/publish-settings`
**Update Publish Settings**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`PublishSettingsUpdate`):
  - `credential_overrides`: object | null
  - `ai_credentials`: _AICredentialDraft | null

**Response:** `AgentPublic`

---

## GET `/api/v1/agents/{agent_id}/setup-status`
**Get Setup Status**

**Path parameters:**
- `agent_id`: uuid

**Response:** `SetupStatusResponse`

---

## GET `/api/v1/agents/{agent_id}/setup-credentials`
**List Setup Credentials**

**Path parameters:**
- `agent_id`: uuid

---

## GET `/api/v1/agents/{agent_id}/bundle-credential-drift`
**Get Bundle Credential Drift**

**Path parameters:**
- `agent_id`: uuid

**Response:** `BundleCredentialDrift`

---

## PUT `/api/v1/agents/{agent_id}/setup-credentials/{credential_id}`
**Update Setup Credential**

**Path parameters:**
- `agent_id`: uuid
- `credential_id`: uuid

**Request body** (`CredentialUpdate`):
  - `name`: string | null
  - `notes`: string | null
  - `credential_data`: object | null
  - `allow_sharing`: boolean | null
  - `allow_template_sharing`: boolean | null
  - `template_private_fields`: array | null
  - `service_uri`: string | null
  - `mcp_mode_conversation`: boolean | null
  - `mcp_mode_building`: boolean | null

**Response:** `CredentialPublic`

---
