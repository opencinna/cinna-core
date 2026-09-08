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

## POST `/api/v1/agents/{agent_id}/skills/{name}/publish`
**Publish Agent Skill**

**Path parameters:**
- `agent_id`: uuid
- `name`: string

**Request body** (`SkillPublishRequest`):
  - `version`: string | null
  - `release_notes`: string | null
  - `visibility`: string | null
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
