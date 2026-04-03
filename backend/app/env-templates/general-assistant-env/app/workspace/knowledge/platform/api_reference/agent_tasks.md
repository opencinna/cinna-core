# Agent Tasks — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-tasks`

## POST `/api/v1/agent/tasks/create`
**Agent Create Task**

**Request body** (`AgentTaskCreate`):
  - `title`: string (required)
  - `description`: string | null
  - `assigned_to`: string | null
  - `priority`: string
  - `source_session_id`: string | null

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

**Response:** `TaskCommentPublic`

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

**Response:** `TaskCommentPublic`

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
