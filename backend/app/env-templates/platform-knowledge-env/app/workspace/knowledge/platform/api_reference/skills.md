# Skills — API Reference

Auto-generated from OpenAPI spec. Tag: `skills`

## GET `/api/v1/skills/catalog`
**List Skill Catalog**

**Response:** `SkillPackagesPublic`

---

## GET `/api/v1/skills/packages/{package_id}`
**Get Skill Package**

**Path parameters:**
- `package_id`: uuid

**Response:** `SkillPackageDetailPublic`

---

## PATCH `/api/v1/skills/packages/{package_id}`
**Update Skill Package**

**Path parameters:**
- `package_id`: uuid

**Request body** (`SkillPackageUpdate`):
  - `display_name`: string | null
  - `description`: string | null
  - `visibility`: string | null
  - `is_listed`: boolean | null

**Response:** `SkillPackageEntry`

---

## POST `/api/v1/skills/packages/{package_id}/delist`
**Delist Skill Package**

**Path parameters:**
- `package_id`: uuid

**Response:** `SkillPackageEntry`

---

## GET `/api/v1/skills/packages/{package_id}/revisions/{revision_number}/content`
**Get Skill Package Revision Content**

**Path parameters:**
- `package_id`: uuid
- `revision_number`: integer

**Response:** `SkillRevisionContentPublic`

---

## GET `/api/v1/skills/packages/{package_id}/revisions/{revision_number}/archive`
**Download Skill Package Archive**

**Path parameters:**
- `package_id`: uuid
- `revision_number`: integer

---

## GET `/api/v1/skills/packages/{package_id}/grants`
**List Skill Package Grants**

**Path parameters:**
- `package_id`: uuid

**Response:** `SkillPackageAccessGrantsPublic`

---

## POST `/api/v1/skills/packages/{package_id}/grants`
**Add Skill Package Grant**

**Path parameters:**
- `package_id`: uuid

**Request body** (`SkillPackageAccessGrantCreate`):
  - `email`: string (required)

**Response:** `SkillPackageAccessGrantPublic`

---

## DELETE `/api/v1/skills/packages/{package_id}/grants/{user_id}`
**Revoke Skill Package Grant**

**Path parameters:**
- `package_id`: uuid
- `user_id`: uuid

---

## GET `/api/v1/agents/{agent_id}/skills/{name}/publish-preview`
**Preview Agent Skill Publish**

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Response:** `SkillPublishPreview`

---

## POST `/api/v1/agents/{agent_id}/skills/{name}/publish`
**Publish Agent Skill**

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Request body** (`SkillPublishRequest`):
  - `version`: string | null
  - `release_notes`: string | null
  - `visibility`: string | null
  - `grant_emails`: string[]
  - `package_id`: string | null

**Response:** `SkillPackageRevisionPublic`

---

## POST `/api/v1/agents/{agent_id}/skills/install`
**Install Agent Skill**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`SkillInstallRequest`):
  - `package_id`: uuid (required)
  - `revision_number`: integer | null
  - `conversation_mode`: boolean
  - `building_mode`: boolean

**Response:** `PluginSyncResponse`

---
