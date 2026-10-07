# Agent Tasks — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-tasks`

## GET `/api/v1/agent/tasks/protocol/targets`
**Tasks Targets**

**Query parameters:**
- `source_session_id`: uuid (required)

**Response:** `TaskTargetsPublic`

---

## GET `/api/v1/agent/tasks/protocol/list`
**Tasks List**

**Query parameters:**
- `source_session_id`: uuid (required)
- `relation`: "assigned" | "created" | "delegated" | "all", default: `all`
- `category`: string | null
- `include_closed`: boolean, default: `False`

---

## GET `/api/v1/agent/tasks/protocol/get`
**Tasks Get**

**Query parameters:**
- `source_session_id`: uuid (required)
- `ref`: string (required)

---

## POST `/api/v1/agent/tasks/protocol/create`
**Tasks Create**

**Request body** (`TasksCreateRequest`):
  - `source_session_id`: uuid (required)
  - `title`: string (required)
  - `description`: string (required)
  - `priority`: string | null
  - `category`: string | null
  - `parent`: string | null
  - `assignee`: string | null
  - `id`: string | null
  - `execution`: string | null
  - `group`: string | null
  - `custom_fields`: object | null

---

## POST `/api/v1/agent/tasks/protocol/update`
**Tasks Update**

**Request body** (`TasksUpdateRequest`):
  - `source_session_id`: uuid (required)
  - `ref`: string (required)
  - `title`: string | null
  - `description`: string | null
  - `priority`: string | null
  - `custom_fields`: object | null
  - `category`: string | null

---

## POST `/api/v1/agent/tasks/protocol/comment`
**Tasks Comment**

**Request body** (`TasksCommentRequest`):
  - `source_session_id`: uuid (required)
  - `ref`: string | null
  - `body`: string (required)
  - `files`: array | null

---

## POST `/api/v1/agent/tasks/protocol/report`
**Tasks Report**

**Request body** (`TasksReportRequest`):
  - `source_session_id`: uuid (required)
  - `ref`: string | null
  - `status`: "in_progress" | "blocked" | "done" | "failed" (required)
  - `summary`: string (required)
  - `question`: string | null
  - `audience`: string | null
  - `artifacts`: string | object[]
  - `body`: string

---

## POST `/api/v1/agent/tasks/protocol/reply`
**Tasks Reply**

**Request body** (`TasksReplyRequest`):
  - `source_session_id`: uuid (required)
  - `ref`: string (required)
  - `message`: string (required)

---

## POST `/api/v1/agent/tasks/current/delegation-result`
**Agent Report Delegation**

**Request body** (`AgentDelegationReport`):
  - `status`: "in_progress" | "blocked" | "done" | "failed" (required)
  - `summary`: string (required)
  - `question`: string | null
  - `audience`: "requester" | "user"
  - `artifacts`: DelegationArtifact[]
  - `body`: string
  - `source_session_id`: uuid (required)

**Response:** `DelegationResultPublic`

---

## POST `/api/v1/agent/tasks/create`
**Agent Create Task**

**Request body** (`AgentTaskCreate`):
  - `title`: string (required)
  - `description`: string | null
  - `assigned_to`: string | null
  - `priority`: string
  - `source_session_id`: string | null
  - `assigned_agent_id`: string | null
  - `assigned_node_id`: string | null
  - `external_ref`: string | null
  - `own_queue`: boolean

**Response:** `AgentTaskOperationResponse`

---

## GET `/api/v1/agent/tasks/by-code/{short_code}`
**Agent Resolve Task By Code**

**Path parameters:**
- `short_code`: string

---

## POST `/api/v1/agent/tasks/current/comment`
**Agent Add Comment Current**

**Request body** (`AgentTaskCommentCreate`):
  - `content`: string (required)
  - `comment_type`: string
  - `file_paths`: array | null
  - `source_session_id`: string | null

**Response:** `AgentCommentResponse`

---

## POST `/api/v1/agent/tasks/current/status`
**Agent Update Status Current**

**Request body** (`AgentTaskStatusUpdate`):
  - `status`: string (required)
  - `reason`: string | null
  - `task`: string | null
  - `source_session_id`: string | null

**Response:** `AgentTaskOperationResponse`

---

## GET `/api/v1/agent/tasks/current/details`
**Agent Get Task Details Current**

**Query parameters:**
- `source_session_id`: uuid (required)

---

## POST `/api/v1/agent/tasks/current/subtask`
**Agent Create Subtask Current**

**Request body** (`AgentSubtaskCreate`):
  - `title`: string (required)
  - `description`: string | null
  - `assigned_to`: string | null
  - `priority`: string
  - `task`: string | null
  - `source_session_id`: string | null
  - `external_ref`: string | null
  - `own_queue`: boolean

**Response:** `AgentTaskOperationResponse`

---

## POST `/api/v1/agent/tasks/{task_id}/comment`
**Agent Add Comment**

**Path parameters:**
- `task_id`: uuid

**Request body** (`AgentTaskCommentCreate`):
  - `content`: string (required)
  - `comment_type`: string
  - `file_paths`: array | null
  - `source_session_id`: string | null

**Response:** `AgentCommentResponse`

---

## POST `/api/v1/agent/tasks/{task_id}/status`
**Agent Update Status**

**Path parameters:**
- `task_id`: uuid

**Request body** (`AgentTaskStatusUpdate`):
  - `status`: string (required)
  - `reason`: string | null
  - `task`: string | null
  - `source_session_id`: string | null

**Response:** `AgentTaskOperationResponse`

---

## POST `/api/v1/agent/tasks/{task_id}/subtask`
**Agent Create Subtask**

**Path parameters:**
- `task_id`: uuid

**Request body** (`AgentSubtaskCreate`):
  - `title`: string (required)
  - `description`: string | null
  - `assigned_to`: string | null
  - `priority`: string
  - `task`: string | null
  - `source_session_id`: string | null
  - `external_ref`: string | null
  - `own_queue`: boolean

**Response:** `AgentTaskOperationResponse`

---

## GET `/api/v1/agent/tasks/my-tasks`
**Agent List Tasks**

**Query parameters:**
- `status`: string | null
- `scope`: string, default: `assigned`

**Response:** `InputTasksPublicExtended`

---

## GET `/api/v1/agent/tasks/{task_id}/details`
**Agent Get Task Details**

**Path parameters:**
- `task_id`: uuid

**Query parameters:**
- `source_session_id`: string | null

---
