# Agents — API Reference

Auto-generated from OpenAPI spec. Tag: `agents`

## GET `/api/v1/agents/status`
**List Agent Statuses**

**Query parameters:**
- `workspace_id`: string | null

**Response:** `AgentStatusListPublic`

---

## GET `/api/v1/agents/{agent_id}/status`
**Get Agent Status**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `force_refresh`: boolean, default: `False`

**Response:** `AgentStatusPublic`

---

## GET `/api/v1/agents/`
**Read Agents**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `user_workspace_id`: string | null

**Response:** `AgentsPublic`

---

## POST `/api/v1/agents/`
**Create Agent**

**Request body** (`AgentCreate`):
  - `name`: string (required)
  - `workflow_prompt`: string | null
  - `entrypoint_prompt`: string | null
  - `refiner_prompt`: string | null
  - `description`: string | null
  - `user_workspace_id`: string | null

**Response:** `AgentPublic`

---

## GET `/api/v1/agents/{id}`
**Read Agent**

**Path parameters:**
- `id`: uuid

**Response:** `AgentPublic`

---

## PUT `/api/v1/agents/{id}`
**Update Agent**

**Path parameters:**
- `id`: uuid

**Request body** (`AgentUpdate`):
  - `name`: string | null
  - `description`: string | null
  - `workflow_prompt`: string | null
  - `entrypoint_prompt`: string | null
  - `refiner_prompt`: string | null
  - `is_active`: boolean | null
  - `ui_color_preset`: string | null
  - `show_on_dashboard`: boolean | null
  - `conversation_mode_ui`: string | null
  - `a2a_config`: object | null
  - `example_prompts`: array | null
  - `inactivity_period_limit`: string | null
  - `webapp_enabled`: boolean | null
  - `update_mode`: string | null

**Response:** `AgentPublic`

---

## DELETE `/api/v1/agents/{id}`
**Delete Agent**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/create-flow`
**Create Agent With Flow**

**Request body** (`AgentCreateFlowRequest`):
  - `description`: string (required)
  - `mode`: string
  - `auto_create_session`: boolean
  - `user_workspace_id`: string | null
  - `agent_sdk_conversation`: string | null
  - `agent_sdk_building`: string | null

**Response:** `AgentCreateFlowResponse`

---

## POST `/api/v1/agents/{id}/sync-prompts`
**Sync Agent Prompts**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agents/{id}/credentials`
**Read Agent Credentials**

**Path parameters:**
- `id`: uuid

**Response:** `CredentialsPublic`

---

## POST `/api/v1/agents/{id}/credentials`
**Add Credential To Agent**

**Path parameters:**
- `id`: uuid

**Request body** (`AgentCredentialLinkRequest`):
  - `credential_id`: uuid (required)

**Response:** `Message`

---

## DELETE `/api/v1/agents/{id}/credentials/{credential_id}`
**Remove Credential From Agent**

**Path parameters:**
- `id`: uuid
- `credential_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/{id}/environments`
**Create Agent Environment**

**Path parameters:**
- `id`: uuid

**Request body** (`AgentEnvironmentCreate`):
  - `env_name`: string (required)
  - `env_version`: string
  - `instance_name`: string
  - `type`: string
  - `config`: object
  - `agent_sdk_conversation`: string | null
  - `agent_sdk_building`: string | null
  - `model_override_conversation`: string | null
  - `model_override_building`: string | null
  - `use_default_ai_credentials`: boolean
  - `conversation_ai_credential_id`: string | null
  - `building_ai_credential_id`: string | null

**Response:** `AgentEnvironmentPublic`

---

## GET `/api/v1/agents/{id}/environments`
**List Agent Environments**

**Path parameters:**
- `id`: uuid

**Response:** `AgentEnvironmentsPublic`

---

## POST `/api/v1/agents/{id}/environments/{env_id}/activate`
**Activate Environment**

**Path parameters:**
- `id`: uuid
- `env_id`: uuid

**Response:** `AgentPublic`

---

## POST `/api/v1/agents/{id}/schedules/generate`
**Generate Schedule**

**Path parameters:**
- `id`: uuid

**Request body** (`ScheduleRequest`):
  - `natural_language`: string (required)
  - `timezone`: string (required)

**Response:** `ScheduleResponse`

---

## POST `/api/v1/agents/{id}/schedules`
**Create Schedule**

**Path parameters:**
- `id`: uuid

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

## GET `/api/v1/agents/{id}/schedules`
**List Schedules**

**Path parameters:**
- `id`: uuid

**Response:** `AgentSchedulesPublic`

---

## PUT `/api/v1/agents/{id}/schedules/{schedule_id}`
**Update Schedule**

**Path parameters:**
- `id`: uuid
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

## DELETE `/api/v1/agents/{id}/schedules/{schedule_id}`
**Delete Schedule**

**Path parameters:**
- `id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/{id}/schedules/{schedule_id}/run`
**Run Schedule Now**

**Path parameters:**
- `id`: uuid
- `schedule_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/agents/{id}/schedules/{schedule_id}/logs`
**List Schedule Logs**

**Path parameters:**
- `id`: uuid
- `schedule_id`: uuid

**Response:** `AgentScheduleLogsPublic`

---

## GET `/api/v1/agents/{id}/handovers`
**List Handover Configs**

**Path parameters:**
- `id`: uuid

**Response:** `HandoverConfigsPublic`

---

## POST `/api/v1/agents/{id}/handovers`
**Create Handover Config**

**Path parameters:**
- `id`: uuid

**Request body** (`HandoverConfigCreate`):
  - `target_agent_id`: uuid (required)
  - `handover_prompt`: string

**Response:** `HandoverConfigPublic`

---

## PUT `/api/v1/agents/{id}/handovers/{handover_id}`
**Update Handover Config**

**Path parameters:**
- `id`: uuid
- `handover_id`: uuid

**Request body** (`HandoverConfigUpdate`):
  - `handover_prompt`: string | null
  - `enabled`: boolean | null

**Response:** `HandoverConfigPublic`

---

## DELETE `/api/v1/agents/{id}/handovers/{handover_id}`
**Delete Handover Config**

**Path parameters:**
- `id`: uuid
- `handover_id`: uuid

**Response:** `Message`

---

## POST `/api/v1/agents/{id}/handovers/generate`
**Generate Handover Prompt Endpoint**

**Path parameters:**
- `id`: uuid

**Request body** (`GenerateHandoverPromptRequest`):
  - `target_agent_id`: uuid (required)

**Response:** `GenerateHandoverPromptResponse`

---

## POST `/api/v1/agents/tasks/create`
**Create Agent Task**

**Request body** (`CreateAgentTaskRequest`):
  - `task_message`: string (required)
  - `target_agent_id`: string | null
  - `target_agent_name`: string | null
  - `source_session_id`: uuid (required)

**Response:** `CreateAgentTaskResponse`

---

## POST `/api/v1/agents/handover/execute`
**Execute Handover**

**Request body** (`ExecuteHandoverRequest`):
  - `task_message`: string (required)
  - `target_agent_id`: string | null
  - `target_agent_name`: string | null
  - `source_session_id`: uuid (required)

**Response:** `ExecuteHandoverResponse`

---

## GET `/api/v1/agents/{id}/sdk-config`
**Get Sdk Config**

**Path parameters:**
- `id`: uuid

**Response:** `AgentSdkConfig`

---

## PATCH `/api/v1/agents/{id}/allowed-tools`
**Add Allowed Tools**

**Path parameters:**
- `id`: uuid

**Request body** (`AllowedToolsUpdate`):
  - `tools`: string[] (required)

**Response:** `AgentSdkConfig`

---

## GET `/api/v1/agents/{id}/pending-tools`
**Get Pending Tools**

**Path parameters:**
- `id`: uuid

**Response:** `PendingToolsResponse`

---

## POST `/api/v1/agents/sessions/update-state`
**Update Session State**

**Request body** (`UpdateSessionStateRequest`):
  - `session_id`: string (required)
  - `state`: string (required)
  - `summary`: string (required)

**Response:** `UpdateSessionStateResponse`

---

## POST `/api/v1/agents/tasks/respond`
**Respond To Task**

**Request body** (`RespondToTaskRequest`):
  - `task_id`: string (required)
  - `message`: string (required)
  - `source_session_id`: string (required)

**Response:** `UpdateSessionStateResponse`

---
