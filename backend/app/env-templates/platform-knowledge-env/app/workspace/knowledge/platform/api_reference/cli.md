# Cli — API Reference

Auto-generated from OpenAPI spec. Tag: `cli`

## POST `/api/v1/cli/setup-tokens`
**Create Setup Token**

> Not available via `cinna api` (excluded_path).

**Request body** (`CLISetupTokenCreate`):
  - `agent_id`: uuid (required)

**Response:** `CLISetupTokenCreated`

---

## GET `/api/v1/cli/tokens`
**List Cli Tokens**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `agent_id`: string | null

**Response:** `CLITokensPublic`

---

## DELETE `/api/v1/cli/tokens/{token_id}`
**Revoke Cli Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/agents/{agent_id}/building-context`
**Get Building Context**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

---

## GET `/api/v1/cli/agents/{agent_id}/workspace`
**Get Workspace**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/knowledge/search`
**Search Knowledge**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`KnowledgeSearchBody`):
  - `query`: string (required)
  - `topic`: string | null

---

## GET `/api/v1/cli/git-coordinates`
**Cli Git Coordinates**

> Not available via `cinna api` (excluded_path).

**Response:** `CliGitCoordinates`

---

## GET `/api/v1/cli/agents/{agent_id}/sync-runtime`
**Get Sync Runtime**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/exec`
**Exec Command**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ExecBody`):
  - `command`: string (required)
  - `timeout`: integer | null

---

## POST `/api/v1/cli/account/setup-tokens`
**Create Account Setup Token**

> Not available via `cinna api` (excluded_path).

**Response:** `CLISetupTokenCreated`

---

## GET `/api/v1/cli/account/tokens`
**List Account Tokens**

> Not available via `cinna api` (excluded_path).

**Response:** `CLIAccountTokensPublic`

---

## DELETE `/api/v1/cli/account/tokens/{token_id}`
**Revoke Account Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/account/agents`
**List Account Agents**

> Not available via `cinna api` (excluded_path).

**Response:** `AccountAgentsPublic`

---

## POST `/api/v1/cli/account/agents`
**Account Create Agent**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentCreateBody`):
  - `name`: string (required)
  - `description`: string | null
  - `env_name`: string | null
  - `user_workspace_id`: string | null
  - `engine`: string | null

**Response:** `AgentPublic`

---

## GET `/api/v1/cli/account/user-workspaces`
**List Account User Workspaces**

> Not available via `cinna api` (excluded_path).

**Response:** `UserWorkspacesPublic`

---

## GET `/api/v1/cli/account/context-package`
**Get Account Context Package**

> Not available via `cinna api` (excluded_path).

---

## GET `/api/v1/cli/account/context-package/version`
**Get Account Context Package Version**

> Not available via `cinna api` (excluded_path).

**Response:** `ContextPackageVersionPublic`

---

## POST `/api/v1/cli/account/knowledge/search`
**Account Search Knowledge**

> Not available via `cinna api` (excluded_path).

**Request body** (`KnowledgeSearchBody`):
  - `query`: string (required)
  - `topic`: string | null

---

## POST `/api/v1/cli/account/files/upload`
**Account Upload File**

> Not available via `cinna api` (excluded_path).

**Request body** (`Body_cli-account_upload_file`):
  - `file`: binary (required)

**Response:** `FileUploadPublic`

---

## POST `/api/v1/cli/account/agents/{agent_id}/mint`
**Mint Child Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`MintChildTokenBody`):
  - `machine_name`: string
  - `machine_info`: string | null

---

## DELETE `/api/v1/cli/account/tokens/children/{child_token_id}`
**Revoke Account Child Token**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `child_token_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/cli/account/connect/agent-api`
**Account Connect Agent Api**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountConnectAgentApiBody`):
  - `producer_agent_id`: uuid (required)
  - `consumer_agent_id`: string | null
  - `credential_label`: string | null
  - `read_only_override`: boolean

**Response:** `ConnectAgentApiResponse`

---

## POST `/api/v1/cli/account/agent-api/enable`
**Account Agent Api Enable**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiEnableBody`):
  - `agent_id`: uuid (required)
  - `enabled`: boolean

---

## POST `/api/v1/cli/account/agent-api/refresh`
**Account Agent Api Refresh**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiRefreshBody`):
  - `agent_id`: uuid (required)

---

## GET `/api/v1/cli/account/agent-api/spec`
**Account Agent Api Spec**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `agent_id`: uuid (required)

---

## POST `/api/v1/cli/account/agent-api/call`
**Account Agent Api Call**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiCallBody`):
  - `agent_id`: uuid (required)
  - `method`: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS"
  - `path`: string (required)
  - `query`: object | null
  - `json_body`: any | null

**Response:** `AccountAgentApiCallResult`

---

## GET `/api/v1/cli/account/agent-api/access`
**Account Agent Api Access**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `agent_id`: uuid (required)

**Response:** `AccountAgentApiAccess`

---

## POST `/api/v1/cli/account/agent-api/keys`
**Account Agent Api Issue Key**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiKeyIssueBody`):
  - `agent_id`: uuid (required)
  - `issued_to_email`: string | null
  - `scopes`: array | null
  - `expires_in_days`: integer | null
  - `service_uri`: string | null
  - `label`: string | null
  - `read_only`: boolean

**Response:** `AccountAgentApiKeyIssued`

---

## POST `/api/v1/cli/account/agent-api/keys/revoke`
**Account Agent Api Revoke Key**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiKeyRevokeBody`):
  - `agent_id`: uuid (required)
  - `key_id`: uuid (required)

**Response:** `AgentApiKeyPublic`

---

## PUT `/api/v1/cli/account/agent-api/access/scopes`
**Account Agent Api Set Scopes**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountAgentApiScopesBody`):
  - `agent_id`: uuid (required)
  - `user_email`: string | null
  - `scopes`: array | null (required)

**Response:** `AccountAgentApiScopesResult`

---

## POST `/api/v1/cli/account/agents/{agent_id}/restart-env`
**Account Restart Env**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Response:** `AccountRestartEnvResult`

---

## POST `/api/v1/cli/account/agents/{agent_id}/rebuild-env`
**Account Rebuild Env**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Response:** `AccountRebuildEnvResult`

---

## GET `/api/v1/cli/account/agents/{agent_id}/inspect`
**Account Inspect Agent**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Response:** `AccountAgentInspectResult`

---

## GET `/api/v1/cli/account/connect/mcp/discoverable`
**Account List Discoverable Mcp**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `consumer_agent_id`: string | null

**Response:** `DiscoverableAgents`

---

## POST `/api/v1/cli/account/connect/mcp`
**Account Connect Mcp**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountConnectMcpBody`):
  - `connector_id`: uuid (required)
  - `consumer_agent_id`: string | null
  - `mcp_mode_conversation`: boolean
  - `mcp_mode_building`: boolean
  - `label`: string | null

**Response:** `MCPProviderConnectionResponse`

---

## GET `/api/v1/cli/account/agents/{agent_id}/schedules`
**Account List Schedules**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentSchedulesPublic`

---

## POST `/api/v1/cli/account/agents/{agent_id}/schedules`
**Account Create Schedule**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`CreateScheduleRequest`):
  - `name`: string (required)
  - `cron_string`: string (required)
  - `timezone`: string (required)
  - `description`: string (required)
  - `prompt`: string | null
  - `enabled`: boolean
  - `schedule_type`: string
  - `command`: string | null

**Response:** `AgentSchedulePublic`

---

## POST `/api/v1/cli/account/agents/{agent_id}/schedules/generate`
**Account Generate Schedule**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ScheduleRequest`):
  - `natural_language`: string (required)
  - `timezone`: string (required)
  - `schedule_type`: string

**Response:** `ScheduleResponse`

---

## PUT `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}`
**Account Update Schedule**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Request body** (`UpdateScheduleRequest`):
  - `name`: string | null
  - `cron_string`: string | null
  - `timezone`: string | null
  - `description`: string | null
  - `prompt`: string | null
  - `enabled`: boolean | null
  - `command`: string | null

**Response:** `AgentSchedulePublic`

---

## DELETE `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}`
**Account Delete Schedule**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/run`
**Account Run Schedule**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/logs`
**Account Schedule Logs**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `AgentScheduleLogsPublic`

---

## GET `/api/v1/cli/account/agents/{agent_id}/status`
**Account Agent Status**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `force_refresh`: boolean, default: `False`

**Response:** `AccountAgentStatusResult`

---

## POST `/api/v1/cli/account/agents/{agent_id}/status/refresh-command`
**Account Set Status Refresh Command**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AccountStatusRefreshCommandBody`):
  - `command`: string | null

**Response:** `AccountAgentStatusResult`

---

## GET `/api/v1/cli/account/credentials/types`
**Account List Credential Types**

> Not available via `cinna api` (excluded_path).

**Response:** `AccountCredentialTypesPublic`

---

## GET `/api/v1/cli/account/credentials`
**Account List Credentials**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `user_workspace_id`: string | null

**Response:** `CredentialsPublic`

---

## POST `/api/v1/cli/account/credentials`
**Account Create Credential**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountCredentialCreateBody`):
  - `name`: string (required)
  - `type`: CredentialType (required)
  - `notes`: string | null
  - `service_uri`: string | null
  - `allow_sharing`: boolean
  - `user_workspace_id`: string | null

**Response:** `AccountCredentialDraftResult`

---

## PUT `/api/v1/cli/account/credentials/{credential_id}`
**Account Update Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AccountCredentialUpdateBody`):
  - `name`: string | null
  - `notes`: string | null
  - `service_uri`: string | null
  - `allow_sharing`: boolean | null
  - `allow_template_sharing`: boolean | null

**Response:** `CredentialPublic`

---

## DELETE `/api/v1/cli/account/credentials/{credential_id}`
**Account Delete Credential**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/cli/account/credentials/{credential_id}/share-with-agent`
**Account Share Credential With Agent**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AccountCredentialShareBody`):
  - `agent_id`: uuid (required)

**Response:** `Message`

---

## GET `/api/v1/cli/account/improvement-requests`
**List Account Improvement Requests**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `status`: string | null
- `agent_id`: string | null
- `skip`: integer, default: `0`
- `limit`: integer, default: `50`

**Response:** `ImprovementRequestsPublic`

---

## GET `/api/v1/cli/account/improvement-requests/{request_id}`
**Get Account Improvement Request**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `request_id`: uuid

**Response:** `ImprovementRequestDetailPublic`

---

## PATCH `/api/v1/cli/account/improvement-requests/{request_id}`
**Update Account Improvement Request**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `request_id`: uuid

**Request body** (`ImprovementRequestUpdate`):
  - `status`: string | null
  - `resolution_note`: string | null

**Response:** `ImprovementRequestDetailPublic`

---

## GET `/api/v1/cli/account/improvement-requests/{request_id}/archive`
**Download Account Improvement Archive**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `request_id`: uuid

---

## POST `/api/v1/cli/account/api-proxy`
**Account Api Proxy**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountApiProxyRequest`):
  - `method`: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" (required)
  - `path`: string (required)
  - `query`: object | null
  - `json_body`: any | null
  - `headers`: object | null

---

## POST `/api/v1/cli/account/login/start`
**Device Login Start**

> Not available via `cinna api` (excluded_path).

**Request body** (`DeviceLoginStartRequest`):
  - `machine_name`: string (required)
  - `machine_info`: string | null

**Response:** `DeviceLoginStartResponse`

---

## POST `/api/v1/cli/account/login/poll`
**Device Login Poll**

> Not available via `cinna api` (excluded_path).

**Request body** (`DeviceLoginPollRequest`):
  - `device_code`: string (required)

**Response:** `DeviceLoginPollResponse`

---

## GET `/api/v1/cli/account/login/request`
**Device Login Request Metadata**

> Not available via `cinna api` (excluded_path).

**Query parameters:**
- `user_code`: string (required)

**Response:** `DeviceLoginRequestPublic`

---

## POST `/api/v1/cli/account/login/approve`
**Device Login Approve**

> Not available via `cinna api` (excluded_path).

**Request body** (`DeviceLoginResolveBody`):
  - `user_code`: string (required)

**Response:** `Message`

---

## POST `/api/v1/cli/account/login/reject`
**Device Login Reject**

> Not available via `cinna api` (excluded_path).

**Request body** (`DeviceLoginResolveBody`):
  - `user_code`: string (required)

**Response:** `Message`

---

## POST `/api/v1/cli/account/desktop-token`
**Exchange Account Token For Desktop Token**

> Not available via `cinna api` (excluded_path).

**Request body** (`AccountDesktopTokenBody`):
  - `client_id`: string | null
  - `device_name`: string | null
  - `platform`: string | null
  - `app_version`: string | null

**Response:** `AccountDesktopTokenResponse`

---

## GET `/api/cli-setup/{token}`
**Get Bootstrap Script**

**Path parameters:**
- `token`: string

---

## POST `/api/cli-setup/{token}`
**Exchange Setup Token**

**Path parameters:**
- `token`: string

**Request body** (`ExchangeSetupTokenBody`):
  - `machine_name`: string
  - `machine_info`: string | null

---

## GET `/api/cli-setup/account/{token}`
**Get Account Bootstrap Script**

**Path parameters:**
- `token`: string

---

## POST `/api/cli-setup/account/{token}`
**Exchange Account Setup Token**

**Path parameters:**
- `token`: string

**Request body** (`ExchangeSetupTokenBody`):
  - `machine_name`: string
  - `machine_info`: string | null

---
