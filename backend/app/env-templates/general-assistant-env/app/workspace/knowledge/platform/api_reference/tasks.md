# Tasks — API Reference

Auto-generated from OpenAPI spec. Tag: `tasks`

## GET `/api/v1/tasks/by-source-session/{session_id}`
**List Tasks By Source Session**

**Path parameters:**
- `session_id`: uuid

**Response:** `InputTasksPublicExtended`

---

## POST `/api/v1/tasks/`
**Create Task**

**Request body** (`InputTaskCreate`):
  - `original_message`: string (required)
  - `selected_agent_id`: string | null
  - `user_workspace_id`: string | null
  - `agent_initiated`: boolean
  - `auto_execute`: boolean
  - `source_session_id`: string | null
  - `file_ids`: array | null
  - `title`: string | null
  - `priority`: string
  - `team_id`: string | null
  - `assigned_node_id`: string | null
  - `parent_task_id`: string | null

**Response:** `InputTaskPublic`

---

## GET `/api/v1/tasks/`
**List Tasks**

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`
- `status`: string | null
- `user_workspace_id`: string | null
- `root_only`: boolean, default: `False`
- `team_id`: string | null
- `priority`: string | null

**Response:** `InputTasksPublicExtended`

---

## GET `/api/v1/tasks/{id}`
**Get Task**

**Path parameters:**
- `id`: uuid

**Response:** `InputTaskPublicExtended`

---

## PATCH `/api/v1/tasks/{id}`
**Update Task**

**Path parameters:**
- `id`: uuid

**Request body** (`InputTaskUpdate`):
  - `current_description`: string | null
  - `selected_agent_id`: string | null
  - `title`: string | null
  - `priority`: string | null
  - `team_id`: string | null
  - `assigned_node_id`: string | null

**Response:** `InputTaskPublic`

---

## DELETE `/api/v1/tasks/{id}`
**Delete Task**

**Path parameters:**
- `id`: uuid

**Response:** `Message`

---

## POST `/api/v1/tasks/{id}/refine`
**Refine Task**

**Path parameters:**
- `id`: uuid

**Request body** (`RefineTaskRequest`):
  - `user_comment`: string (required)
  - `user_selected_text`: string | null

**Response:** `RefineTaskResponse`

---

## POST `/api/v1/tasks/{id}/execute`
**Execute Task**

**Path parameters:**
- `id`: uuid

**Request body** (`ExecuteTaskRequest`):
  - `mode`: string

**Response:** `ExecuteTaskResponse`

---

## POST `/api/v1/tasks/{id}/send-answer`
**Send Task Email Answer**

**Path parameters:**
- `id`: uuid

**Request body** (`SendAnswerRequest`):
  - `custom_message`: string | null

**Response:** `SendAnswerResponse`

---

## POST `/api/v1/tasks/{id}/archive`
**Archive Task**

**Path parameters:**
- `id`: uuid

**Response:** `InputTaskPublic`

---

## GET `/api/v1/tasks/{id}/sessions`
**List Task Sessions**

**Path parameters:**
- `id`: uuid

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `20`

**Response:** `SessionsPublic`

---

## POST `/api/v1/tasks/{id}/files/{file_id}`
**Attach File To Task**

**Path parameters:**
- `id`: uuid
- `file_id`: uuid

**Response:** `FileUploadPublic`

---

## DELETE `/api/v1/tasks/{id}/files/{file_id}`
**Detach File From Task**

**Path parameters:**
- `id`: uuid
- `file_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/tasks/by-code/{short_code}`
**Get Task By Code**

**Path parameters:**
- `short_code`: string

**Response:** `InputTaskPublicExtended`

---

## GET `/api/v1/tasks/by-code/{short_code}/detail`
**Get Task Detail By Code**

**Path parameters:**
- `short_code`: string

**Response:** `InputTaskDetailPublic`

---

## GET `/api/v1/tasks/by-code/{short_code}/tree`
**Get Task Tree By Code**

**Path parameters:**
- `short_code`: string

---

## GET `/api/v1/tasks/{id}/detail`
**Get Task Detail**

**Path parameters:**
- `id`: uuid

**Response:** `InputTaskDetailPublic`

---

## GET `/api/v1/tasks/{id}/comments/`
**List Task Comments**

**Path parameters:**
- `id`: uuid

**Query parameters:**
- `skip`: integer, default: `0`
- `limit`: integer, default: `100`

**Response:** `TaskCommentsPublic`

---

## POST `/api/v1/tasks/{id}/comments/`
**Add Task Comment**

**Path parameters:**
- `id`: uuid

**Request body** (`TaskCommentCreate`):
  - `content`: string (required)
  - `comment_type`: string

**Response:** `TaskCommentPublic`

---

## DELETE `/api/v1/tasks/{id}/comments/{comment_id}`
**Delete Task Comment**

**Path parameters:**
- `id`: uuid
- `comment_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/tasks/{id}/attachments/`
**List Task Attachments**

**Path parameters:**
- `id`: uuid

**Response:** `TaskAttachmentsPublic`

---

## POST `/api/v1/tasks/{id}/attachments/`
**Upload Task Attachment**

**Path parameters:**
- `id`: uuid

**Request body** (`Body_tasks-upload_task_attachment`):
  - `file`: binary (required)

**Response:** `TaskAttachmentPublic`

---

## GET `/api/v1/tasks/{id}/attachments/{attachment_id}/download`
**Download Task Attachment**

**Path parameters:**
- `id`: uuid
- `attachment_id`: uuid

---

## DELETE `/api/v1/tasks/{id}/attachments/{attachment_id}`
**Delete Task Attachment**

**Path parameters:**
- `id`: uuid
- `attachment_id`: uuid

**Response:** `Message`

---

## GET `/api/v1/tasks/{id}/subtasks/`
**List Subtasks**

**Path parameters:**
- `id`: uuid

**Response:** `InputTasksPublicExtended`

---

## POST `/api/v1/tasks/{id}/subtasks/`
**Create Subtask**

**Path parameters:**
- `id`: uuid

**Request body** (`InputTaskCreate`):
  - `original_message`: string (required)
  - `selected_agent_id`: string | null
  - `user_workspace_id`: string | null
  - `agent_initiated`: boolean
  - `auto_execute`: boolean
  - `source_session_id`: string | null
  - `file_ids`: array | null
  - `title`: string | null
  - `priority`: string
  - `team_id`: string | null
  - `assigned_node_id`: string | null
  - `parent_task_id`: string | null

**Response:** `InputTaskPublic`

---
