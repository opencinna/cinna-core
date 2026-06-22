# Cli — API Reference

Auto-generated from OpenAPI spec. Tag: `cli`

## POST `/api/v1/cli/setup-tokens`
**Create Setup Token**

**Request body** (`CLISetupTokenCreate`):
  - `agent_id`: uuid (required)

**Response:** `CLISetupTokenCreated`

---

## GET `/api/v1/cli/tokens`
**List Cli Tokens**

**Query parameters:**
- `agent_id`: string | null

**Response:** `CLITokensPublic`

---

## DELETE `/api/v1/cli/tokens/{token_id}`
**Revoke Cli Token**

**Path parameters:**
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/agents/{agent_id}/building-context`
**Get Building Context**

**Path parameters:**
- `agent_id`: uuid

---

## GET `/api/v1/cli/agents/{agent_id}/workspace`
**Get Workspace**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/knowledge/search`
**Search Knowledge**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`KnowledgeSearchBody`):
  - `query`: string (required)
  - `topic`: string | null

---

## GET `/api/v1/cli/agents/{agent_id}/sync-runtime`
**Get Sync Runtime**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/cli/agents/{agent_id}/exec`
**Exec Command**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`ExecBody`):
  - `command`: string (required)
  - `timeout`: integer | null

---

## POST `/api/v1/cli/account/setup-tokens`
**Create Account Setup Token**

**Response:** `CLISetupTokenCreated`

---

## GET `/api/v1/cli/account/tokens`
**List Account Tokens**

**Response:** `CLIAccountTokensPublic`

---

## DELETE `/api/v1/cli/account/tokens/{token_id}`
**Revoke Account Token**

**Path parameters:**
- `token_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/account/agents`
**List Account Agents**

**Response:** `AccountAgentsPublic`

---

## POST `/api/v1/cli/account/agents`
**Account Create Agent**

**Request body** (`AccountAgentCreateBody`):
  - `name`: string (required)
  - `description`: string | null
  - `env_name`: string | null
  - `user_workspace_id`: string | null

**Response:** `AgentPublic`

---

## GET `/api/v1/cli/account/user-workspaces`
**List Account User Workspaces**

**Response:** `UserWorkspacesPublic`

---

## GET `/api/v1/cli/account/context-package`
**Get Account Context Package**

---

## POST `/api/v1/cli/account/knowledge/search`
**Account Search Knowledge**

**Request body** (`KnowledgeSearchBody`):
  - `query`: string (required)
  - `topic`: string | null

---

## POST `/api/v1/cli/account/files/upload`
**Account Upload File**

**Request body** (`Body_cli-account_upload_file`):
  - `file`: binary (required)

**Response:** `FileUploadPublic`

---

## POST `/api/v1/cli/account/agents/{agent_id}/mint`
**Mint Child Token**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`MintChildTokenBody`):
  - `machine_name`: string
  - `machine_info`: string | null

---

## DELETE `/api/v1/cli/account/tokens/children/{child_token_id}`
**Revoke Account Child Token**

**Path parameters:**
- `child_token_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/cli/account/connect/agent-api`
**Account Connect Agent Api**

**Request body** (`AccountConnectAgentApiBody`):
  - `producer_agent_id`: uuid (required)
  - `consumer_agent_id`: string | null
  - `credential_label`: string | null
  - `read_only_override`: boolean

**Response:** `ConnectAgentApiResponse`

---

## POST `/api/v1/cli/account/agent-api/enable`
**Account Agent Api Enable**

**Request body** (`AccountAgentApiEnableBody`):
  - `agent_id`: uuid (required)
  - `enabled`: boolean

---

## POST `/api/v1/cli/account/agent-api/refresh`
**Account Agent Api Refresh**

**Request body** (`AccountAgentApiRefreshBody`):
  - `agent_id`: uuid (required)

---

## GET `/api/v1/cli/account/agent-api/spec`
**Account Agent Api Spec**

**Query parameters:**
- `agent_id`: uuid (required)

---

## POST `/api/v1/cli/account/agent-api/call`
**Account Agent Api Call**

**Request body** (`AccountAgentApiCallBody`):
  - `agent_id`: uuid (required)
  - `method`: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS"
  - `path`: string (required)
  - `query`: object | null
  - `json_body`: any | null

**Response:** `AccountAgentApiCallResult`

---

## POST `/api/v1/cli/account/agents/{agent_id}/restart-env`
**Account Restart Env**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AccountRestartEnvResult`

---

## GET `/api/v1/cli/account/agents/{agent_id}/inspect`
**Account Inspect Agent**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AccountAgentInspectResult`

---

## GET `/api/v1/cli/account/connect/mcp/discoverable`
**Account List Discoverable Mcp**

**Query parameters:**
- `consumer_agent_id`: string | null

**Response:** `DiscoverableAgents`

---

## POST `/api/v1/cli/account/connect/mcp`
**Account Connect Mcp**

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

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentSchedulesPublic`

---

## POST `/api/v1/cli/account/agents/{agent_id}/schedules`
**Account Create Schedule**

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

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/run`
**Account Run Schedule**

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/cli/account/agents/{agent_id}/schedules/{schedule_id}/logs`
**Account Schedule Logs**

**Path parameters:**
- `agent_id`: uuid
- `schedule_id`: uuid

**Response:** `AgentScheduleLogsPublic`

---

## GET `/api/v1/cli/account/agents/{agent_id}/status`
**Account Agent Status**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `force_refresh`: boolean, default: `False`

**Response:** `AccountAgentStatusResult`

---

## POST `/api/v1/cli/account/agents/{agent_id}/status/refresh-command`
**Account Set Status Refresh Command**

**Path parameters:**
- `agent_id`: uuid

**Request body** (`AccountStatusRefreshCommandBody`):
  - `command`: string | null

**Response:** `AccountAgentStatusResult`

---

## GET `/api/v1/cli/account/credentials/types`
**Account List Credential Types**

**Response:** `AccountCredentialTypesPublic`

---

## GET `/api/v1/cli/account/credentials`
**Account List Credentials**

**Query parameters:**
- `user_workspace_id`: string | null

**Response:** `CredentialsPublic`

---

## POST `/api/v1/cli/account/credentials`
**Account Create Credential**

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

**Path parameters:**
- `credential_id`: uuid

**Query parameters:**
- `force`: boolean, default: `False`

**Response:** `Message`

---

## POST `/api/v1/cli/account/credentials/{credential_id}/share-with-agent`
**Account Share Credential With Agent**

**Path parameters:**
- `credential_id`: uuid

**Request body** (`AccountCredentialShareBody`):
  - `agent_id`: uuid (required)

**Response:** `Message`

---

## POST `/api/v1/cli/account/api-proxy`
**Account Api Proxy**

**Request body** (`AccountApiProxyRequest`):
  - `method`: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" (required)
  - `path`: string (required)
  - `query`: object | null
  - `json_body`: any | null
  - `headers`: object | null

---

## POST `/api/v1/cli/account/login/start`
**Device Login Start**

**Request body** (`DeviceLoginStartRequest`):
  - `machine_name`: string (required)
  - `machine_info`: string | null

**Response:** `DeviceLoginStartResponse`

---

## POST `/api/v1/cli/account/login/poll`
**Device Login Poll**

**Request body** (`DeviceLoginPollRequest`):
  - `device_code`: string (required)

**Response:** `DeviceLoginPollResponse`

---

## GET `/api/v1/cli/account/login/request`
**Device Login Request Metadata**

**Query parameters:**
- `user_code`: string (required)

**Response:** `DeviceLoginRequestPublic`

---

## POST `/api/v1/cli/account/login/approve`
**Device Login Approve**

**Request body** (`DeviceLoginResolveBody`):
  - `user_code`: string (required)

**Response:** `Message`

---

## POST `/api/v1/cli/account/login/reject`
**Device Login Reject**

**Request body** (`DeviceLoginResolveBody`):
  - `user_code`: string (required)

**Response:** `Message`

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
