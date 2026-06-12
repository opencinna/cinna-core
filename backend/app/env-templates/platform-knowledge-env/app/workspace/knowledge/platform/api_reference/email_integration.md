# Email Integration — API Reference

Auto-generated from OpenAPI spec. Tag: `email-integration`

## GET `/api/v1/agents/{agent_id}/email-integration`
**Get Email Integration**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agents/{agent_id}/email-integration`
**Create Or Update Email Integration**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AgentEmailIntegrationCreate`):
  - `enabled`: boolean
  - `access_mode`: EmailAccessMode
  - `process_as`: EmailProcessAs
  - `auto_approve_email_pattern`: string | null
  - `allowed_domains`: string | null
  - `max_clones`: integer
  - `clone_share_mode`: EmailCloneShareMode
  - `agent_session_mode`: AgentSessionMode
  - `incoming_server_id`: string | null
  - `incoming_mailbox`: string | null
  - `outgoing_server_id`: string | null
  - `outgoing_from_address`: string | null

**Response:** `AgentEmailIntegrationPublic`

---

## DELETE `/api/v1/agents/{agent_id}/email-integration`
**Delete Email Integration**

**Path parameters:**
- `agent_id`: uuid

**Response:** `Message`

---

## PUT `/api/v1/agents/{agent_id}/email-integration/enable`
**Enable Email Integration**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentEmailIntegrationPublic`

---

## PUT `/api/v1/agents/{agent_id}/email-integration/disable`
**Disable Email Integration**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentEmailIntegrationPublic`

---

## POST `/api/v1/agents/{agent_id}/email-integration/process-emails`
**Process Emails**

**Path parameters:**
- `agent_id`: uuid

**Response:** `ProcessEmailsResult`

---
