# External — API Reference

Auto-generated from OpenAPI spec. Tag: `external`

## GET `/api/v1/external/agents`
**List External Agents**

**Query parameters:**
- `workspace_id`: string | null

**Response:** `ExternalAgentListResponse`

---

## POST `/api/v1/external/agents/{agent_id}/check-updates`
**Check External Install Updates**

**Path parameters:**
- `agent_id`: uuid

**Response:** `CheckUpdatesResponse`

---

## POST `/api/v1/external/agents/{agent_id}/apply-update`
**Apply External Install Update**

**Path parameters:**
- `agent_id`: uuid

**Response:** `BundleVersionInfo`

---

## GET `/api/v1/external/sessions`
**List External Sessions**

**Query parameters:**
- `limit`: integer, default: `100`
- `offset`: integer, default: `0`

---

## GET `/api/v1/external/sessions/{session_id}`
**Get External Session**

**Path parameters:**
- `session_id`: uuid

**Response:** `ExternalSessionPublic`

---

## DELETE `/api/v1/external/sessions/{session_id}`
**Hide External Session**

**Path parameters:**
- `session_id`: uuid

---

## GET `/api/v1/external/sessions/{session_id}/messages`
**List External Session Messages**

**Path parameters:**
- `session_id`: uuid

---

## GET `/api/v1/external/account-config`
**Get Account Config**

**Response:** `AccountConfigResponse`

---

## GET `/api/v1/external/credentials`
**List Credentials**

**Query parameters:**
- `include`: string | null

**Response:** `DesktopCredentialList`

---

## POST `/api/v1/external/credentials/materialize`
**Materialize Credentials**

**Query parameters:**
- `include`: string | null

**Request body** (`DesktopCredentialMaterializeRequest`):
  - `credential_ids`: uuid[] (required)
  - `include_current_user`: boolean

**Response:** `DesktopCredentialMaterializeResponse`

---

## POST `/api/v1/external/credentials/oauth`
**Create Oauth Credential**

**Request body** (`DesktopOAuthCredentialCreate`):
  - `type`: CredentialType (required)
  - `name`: string | null

**Response:** `DesktopOAuthAuthorizeResponse`

---

## POST `/api/v1/external/credentials/{credential_id}/oauth/authorize`
**Reauthorize Oauth Credential**

**Path parameters:**
- `credential_id`: uuid

**Response:** `DesktopOAuthAuthorizeResponse`

---

## POST `/api/v1/external/credentials/{credential_id}/access-token`
**Desktop Credential Access Token**

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AgentCredentialAccessTokenRequest`):
  - `min_ttl`: integer
  - `known_expires_at`: integer | null

**Response:** `AgentCredentialAccessTokenResponse`

---
