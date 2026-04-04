# Agent File Management - Technical Details

## File Locations

### Backend

- **Routes:** `backend/app/api/routes/files.py` - File upload, delete, download endpoints
- **Routes:** `backend/app/api/routes/workspace.py` - File viewing endpoint (stream file content)
- **Routes:** `backend/app/api/routes/messages.py` - Message send flow with file attachment
- **Models:** `backend/app/models/files/file_upload.py` - FileUpload, MessageFile, FileUploadPublic, FileUploadCreate
- **Models:** `backend/app/models/sessions/session.py` - MessageCreate (file_ids field), MessagePublic (files field)
- **Services:** `backend/app/services/files/file_service.py` - File upload, transfer, permissions, quota
- **Services:** `backend/app/services/files/file_storage_service.py` - Disk storage, streaming, deletion
- **Services:** `backend/app/services/files/garbage_collection_service.py` - Soft-delete cleanup
- **Services:** `backend/app/services/files/file_cleanup_scheduler.py` - Daily cleanup scheduler (APScheduler)
- **Adapter:** `backend/app/services/environments/adapters/docker_adapter.py` - Agent-env file transfer
- **Config:** `backend/app/core/config.py` - Upload settings (size limits, quotas, MIME types)
- **Migration:** `backend/app/alembic/versions/8510f306b385_add_file_uploads_table.py`
- **App startup:** `backend/app/main.py` - Registers file cleanup scheduler

### Agent-Env (inside Docker container)

- **Routes:** `backend/app/env-templates/app_core_base/core/server/routes.py` - `/files/upload` endpoint
- **Service:** `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - `sanitize_filename()`, `resolve_filename_conflict()`, `get_workspace_tree()`
- **Models:** `backend/app/env-templates/app_core_base/core/server/models.py` - FileUploadResponse, WorkspaceTreeResponse
- **Prompts:** `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` - `_get_environment_context()` adds upload path info to prompts

### Frontend

- **Components:** `frontend/src/components/Chat/FileUploadModal.tsx` - Drag-and-drop upload modal (react-dropzone)
- **Components:** `frontend/src/components/Chat/FileBadge.tsx` - File icon, name, size, download/delete buttons
- **Components:** `frontend/src/components/Chat/MessageInput.tsx` - Paperclip button, attached file badges, file_ids in payload
- **Components:** `frontend/src/components/Chat/MessageBubble.tsx` - Renders file badges for message attachments
- **Components:** `frontend/src/components/Environment/FileViewer.tsx` - Main file viewer with header and download
- **Components:** `frontend/src/components/Environment/CSVViewer.tsx` - CSV table rendering
- **Components:** `frontend/src/components/Environment/MarkdownViewer.tsx` - Markdown with GFM and syntax highlighting
- **Components:** `frontend/src/components/Environment/JSONViewer.tsx` - Collapsible JSON tree with type coloring
- **Components:** `frontend/src/components/Environment/TextViewer.tsx` - Preformatted monospace text
- **Components:** `frontend/src/components/Environment/TreeItemRenderer.tsx` - Makes viewable files clickable in environment panel
- **Routes:** `frontend/src/routes/_layout/environment/$envId/file.tsx` - Authenticated file viewer route
- **Routes:** `frontend/src/routes/guest/file-viewer.tsx` - Guest file viewer route (standalone, not under `_layout/`)
- **Hooks:** `frontend/src/hooks/useSessionStreaming.ts` - `sendMessage` mutation includes `file_ids`

### Infrastructure

- `.gitignore` - `backend/data/uploads/` exclusion
- `docker-compose.yml` - Volume mount for uploads directory

## Database Schema

**Migration:** `backend/app/alembic/versions/8510f306b385_add_file_uploads_table.py`

**Tables:**
- `file_uploads` - File metadata: filename, path, size, mime_type, status (temporary/attached/marked_for_deletion), timestamps, user_id
- `message_files` - Junction table linking messages to files; stores `agent_env_path` after transfer

**Models:** `backend/app/models/files/file_upload.py`
- `FileUpload` (table=True) - Database table model
- `FileUploadPublic` - API response schema
- `FileUploadCreate` - API input schema
- `MessageFile` (table=True) - Junction table model

**Updated models:** `backend/app/models/sessions/session.py`
- `MessageCreate` - Added `file_ids: list[uuid.UUID] | None`
- `MessagePublic` - Added `files: list[FilePublic]`

## API Endpoints

### File Operations - `backend/app/api/routes/files.py`

- `POST /api/v1/files/upload` - Upload file, creates temporary record. Auth: `CurrentUserOrGuest`
- `DELETE /api/v1/files/{file_id}` - Soft delete file. Auth: `CurrentUserOrGuest`
- `GET /api/v1/files/{file_id}/download` - Download file with auth check. Auth: `CurrentUserOrGuest`

### File Viewing - `backend/app/api/routes/workspace.py`

- `GET /api/v1/environments/{env_id}/workspace/view-file/{path}` - Stream file content as text

### Message Send (file integration) - `backend/app/api/routes/messages.py`

- `send_message_stream()` - Validates file ownership/status, transfers to agent-env, creates message with file associations

## Services & Key Methods

### FileService - `backend/app/services/files/file_service.py`

- `create_file_upload()` - Store file to disk, create DB record
- `upload_files_to_agent_env()` - Transfer files to Docker container via adapter
- `mark_files_as_attached()` - Update status to attached after message send
- `mark_file_for_deletion()` - Soft delete
- `check_download_permission()` - Auth logic (owner or session participant)
- `check_user_storage_quota()` - Enforce 10GB limit

### FileStorageService - `backend/app/services/files/file_storage_service.py`

- `store_file()` - Save to `backend/data/uploads/{user_id}/{file_id}/`
- `get_file_path()` - Resolve file path from DB record
- `stream_file()` - Yield file chunks for download
- `delete_file()` - Remove from disk
- `get_user_storage_usage()` - Calculate user's total storage

### GarbageCollectionService - `backend/app/services/files/garbage_collection_service.py`

- `collect_garbage()` - Delete files marked for deletion >24h ago

### FileCleanupScheduler - `backend/app/services/files/file_cleanup_scheduler.py`

- Runs daily at 3 AM via APScheduler
- Triggers garbage collection

### DockerAdapter - `backend/app/services/environments/adapters/docker_adapter.py`

- `upload_file_to_agent_env()` - Async HTTP POST with file content to agent-env `/files/upload`

### Agent-Env Service - `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`

- `sanitize_filename()` - Strip dangerous characters, normalize unicode
- `resolve_filename_conflict()` - Append `_1`, `_2` if file exists

### Agent-Env Upload Endpoint - `backend/app/env-templates/app_core_base/core/server/routes.py`

`POST /files/upload` accepts an optional `subfolder` form field. When provided, the file is placed at `./uploads/{subfolder}/{filename}` instead of `./uploads/{filename}`. The subfolder name is sanitized with `sanitize_filename()` to prevent path traversal.

This is used by the task file auto-upload feature: when an agent calls `mcp__agent_task__get_details`, the backend uploads all task-associated files with `subfolder=task_{SHORT_CODE}` (e.g., `task_HR-5`), placing them at `/app/workspace/uploads/task_HR-5/` in the agent's workspace.

## Frontend Components

### Upload Flow

- `FileUploadModal.tsx` - Modal with react-dropzone; uploads via `POST /api/v1/files/upload`, manages temporary file list
- `FileBadge.tsx` - Displays file icon, name, size; download via `GET /api/v1/files/{file_id}/download`; delete for temporary files
- `MessageInput.tsx` - Paperclip button opens modal; shows attached badges; sends `file_ids` in `MessageCreate`
- `MessageBubble.tsx` - Renders `message.files` array using FileBadge

### File Viewing

- `TreeItemRenderer.tsx` - Detects viewable file types (CSV, Markdown, JSON, TXT, LOG); cursor pointer on hover; opens viewer in new tab via `window.open()`; accepts `isGuest` prop for correct route selection
- `FileViewer.tsx` - Main viewer component with header, filename, path, download button (authenticated route)
- `CSVViewer.tsx` - Table rendering with header row detection, quoted field parsing
- `MarkdownViewer.tsx` - Rendered markdown with GFM, code syntax highlighting
- `JSONViewer.tsx` - Collapsible tree; green strings, orange numbers, purple booleans, red null; expand/collapse all; depth 0-1 expanded by default
- `TextViewer.tsx` - Preformatted monospace text with word wrapping

### Routes

- `frontend/src/routes/_layout/environment/$envId/file.tsx` - Authenticated viewer; params: `envId` (URL), `path` (search)
- `frontend/src/routes/guest/file-viewer.tsx` - Guest viewer; params: `envId`, `path` (both search); standalone header

## Configuration

**Settings in `backend/app/core/config.py`:**

- `UPLOAD_BASE_PATH` - Storage directory (default: `/app/data/uploads`)
- `UPLOAD_MAX_FILE_SIZE_MB` - Per-file limit (default: 100MB)
- `UPLOAD_MAX_USER_STORAGE_GB` - User quota (default: 10GB)
- `UPLOAD_ALLOWED_MIME_TYPES` - MIME type whitelist (PDF, CSV, images, code, etc.)
- Computed properties: `allowed_mime_types`, `upload_max_file_size_bytes`, `upload_max_user_storage_bytes`

## Security

- **Validation:** File size limit, MIME type whitelist, user storage quota, filename sanitization (directory traversal prevention)
- **Access control:** Upload requires auth (`CurrentUserOrGuest`); download requires file owner OR session owner; delete requires file owner only; agent-env upload requires auth token
- **Storage isolation:** UUID-based paths prevent enumeration; files isolated by `user_id`; guest uploads stored under agent owner's `user_id`
- **Soft delete:** Grace period before permanent deletion via garbage collection

## Message Send Integration

The file attachment flow within `backend/app/api/routes/messages.py:send_message_stream()`:

1. Validate environment is running
2. Check file ownership and status (must be `temporary`)
3. Upload files to agent-env via `FileService.upload_files_to_agent_env()`
4. Create user message with `file_ids`
5. Update `message_files.agent_env_path` with returned paths
6. Mark files as attached
7. Compose message with file paths prepended: `"Uploaded files:\n- ./uploads/file.pdf\n---\n\n{user_message}"`
8. Stream to agent

---

*Last updated: 2026-03-02*
