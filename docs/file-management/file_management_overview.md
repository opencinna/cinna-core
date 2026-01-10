# File Upload Feature - Implementation Reference

## Purpose

This document provides an overview of the implemented file upload feature, referencing actual files and key methods for context and navigation.

## Feature Overview

Enables users to upload files to chat messages, store them securely in backend storage, and transfer them to agent Docker environments where agents can access them at `./uploads/` path.

**Flow:**
1. User uploads files via UI modal → stored in backend (`backend/data/uploads/`)
2. User sends message with attached files → files transferred to agent-env (`/app/workspace/uploads/`)
3. Agent receives message with file paths prepended (e.g., `./uploads/document.pdf`)
4. Agent processes files in isolated workspace
5. User can download files from message history

## Architecture

```
Frontend UI → Backend API → Backend Storage → Agent-Env Upload → Agent Access
(React)       (FastAPI)     (DB + Disk)       (Docker API)        (./uploads/)
```

**Storage Locations:**
- **Backend:** `backend/data/uploads/{user_id}/{file_id}/` (actual files)
- **Database:** `file_uploads` + `message_files` tables (metadata)
- **Agent-Env:** `/app/workspace/uploads/` (transferred copies)

## File Lifecycle States

- **temporary** - Uploaded but not attached to message (can be deleted)
- **attached** - Associated with sent message (permanent)
- **marked_for_deletion** - Soft deleted, garbage collected after 24h

## Database Schema

**Migration:** `backend/app/alembic/versions/8510f306b385_add_file_uploads_table.py`

**Tables:**
- `file_uploads` - File metadata (filename, path, size, mime_type, status, timestamps)
- `message_files` - Junction table linking messages to files, stores `agent_env_path`

**Models:** `backend/app/models/file_upload.py`
- `FileUpload` (table model)
- `FileUploadPublic`, `FileUploadCreate` (schemas)
- `MessageFile` (junction table model)

**Updated:** `backend/app/models/session.py`
- `MessageCreate` - Added `file_ids: list[uuid.UUID] | None`
- `MessagePublic` - Added `files: list[FilePublic]`

## Backend Implementation

### API Routes

**File Operations:** `backend/app/api/routes/files.py`
- `POST /api/v1/files/upload` - Upload file (creates temporary record)
- `DELETE /api/v1/files/{file_id}` - Soft delete file
- `GET /api/v1/files/{file_id}/download` - Download file with auth check

**Message Updates:** `backend/app/api/routes/messages.py:send_message_stream()`
- Validates file ownership and status
- Calls `FileService.upload_files_to_agent_env()`
- Creates message with file associations via `MessageService.create_message()`
- Updates `message_files.agent_env_path` after upload
- Marks files as attached via `FileService.mark_files_as_attached()`
- Composes message with file paths prepended for agent

### Services

**FileService:** `backend/app/services/file_service.py`
- `create_file_upload()` - Store file to disk, create DB record
- `upload_files_to_agent_env()` - Transfer files to Docker container
- `mark_files_as_attached()` - Update status to attached
- `mark_file_for_deletion()` - Soft delete
- `check_download_permission()` - Auth logic (owner or session participant)
- `check_user_storage_quota()` - Enforce 10GB limit

**FileStorageService:** `backend/app/services/file_storage_service.py`
- `store_file()` - Save to `backend/data/uploads/{user_id}/{file_id}/`
- `get_file_path()` - Resolve file path from DB record
- `stream_file()` - Yield file chunks for download
- `delete_file()` - Remove from disk
- `get_user_storage_usage()` - Calculate user's total storage

**GarbageCollectionService:** `backend/app/services/garbage_collection_service.py`
- `collect_garbage()` - Delete files marked for deletion >24h ago
- Called by scheduler

**FileCleanupScheduler:** `backend/app/services/file_cleanup_scheduler.py`
- Runs daily at 3 AM via APScheduler
- Triggers garbage collection

### Configuration

**Settings:** `backend/app/core/config.py`
- `UPLOAD_BASE_PATH` - Storage directory (default: `/app/data/uploads`)
- `UPLOAD_MAX_FILE_SIZE_MB` - Per-file limit (default: 100MB)
- `UPLOAD_MAX_USER_STORAGE_GB` - User quota (default: 10GB)
- `UPLOAD_ALLOWED_MIME_TYPES` - Whitelist (PDF, CSV, images, code, etc.)
- Computed properties: `allowed_mime_types`, `upload_max_file_size_bytes`, `upload_max_user_storage_bytes`

**Main App:** `backend/app/main.py`
- Registers file cleanup scheduler on startup
- Router registration in `backend/app/api/main.py`

## Frontend Implementation

### Components

**FileUploadModal:** `frontend/src/components/Chat/FileUploadModal.tsx`
- Modal with drag-and-drop zone (react-dropzone)
- Upload files via `POST /api/v1/files/upload`
- Manages temporary file list with local state
- Delete button calls `DELETE /api/v1/files/{file_id}`

**FileBadge:** `frontend/src/components/Chat/FileBadge.tsx`
- Display file icon, name, size
- Download button calls `GET /api/v1/files/{file_id}/download`
- Delete button for temporary files

**MessageInput:** `frontend/src/components/Chat/MessageInput.tsx`
- Paperclip button opens FileUploadModal
- Shows attached file badges
- Sends `file_ids` array in `MessageCreate` payload

**MessageBubble:** `frontend/src/components/Chat/MessageBubble.tsx`
- Displays file badges for messages with attachments
- Renders `message.files` array using FileBadge component

### State Management

**useMessageStream Hook:** `frontend/src/hooks/useMessageStream.ts`
- `sendMessage` mutation includes `file_ids` in request body

## Agent-Env Implementation

**Routes:** `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`
- `POST /files/upload` - Receive file from backend, save to `/app/workspace/uploads/`
- Uses `AgentEnvService.sanitize_filename()` for security
- Handles conflicts with `AgentEnvService.resolve_filename_conflict()`

**Service Methods:** `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`
- `sanitize_filename()` - Strip dangerous characters, normalize unicode
- `resolve_filename_conflict()` - Append `_1`, `_2`, etc. if file exists
- `get_workspace_tree()` - Updated to include `uploads` folder

**Prompt Updates:** `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py`
- `_get_environment_context()` - Added to system prompts
- Informs agent that uploaded files are at `./uploads/` relative path
- Included in both building and conversation mode prompts

**Models:** `backend/app/env-templates/python-env-advanced/app/core/server/models.py`
- `FileUploadResponse` - Response schema for upload endpoint
- `WorkspaceTreeResponse` - Added `uploads` field

## Security Features

**Validation:**
- File size limit: 100MB (configurable)
- Mime type whitelist (configured in settings)
- User storage quota: 10GB (configurable)
- Filename sanitization prevents directory traversal

**Access Control:**
- Upload: Authenticated users only
- Download: File owner OR session owner (checked in `FileService.check_download_permission()`)
- Agent-env upload: Auth token required

**Storage:**
- UUID-based paths prevent enumeration
- Files isolated by user_id
- Soft delete with grace period

## Key Integration Points

**Message Flow:** `backend/app/api/routes/messages.py:send_message_stream()`
1. Validate environment is running
2. Check file ownership and status (`temporary` only)
3. Upload files to agent-env via `FileService.upload_files_to_agent_env()`
4. Create user message with `file_ids`
5. Update `message_files.agent_env_path`
6. Mark files as attached
7. Compose message with file paths prepended: `"Uploaded files:\n- ./uploads/file.pdf\n---\n\n{user_message}"`
8. Stream to agent

**Backend to Agent-Env Transfer:** `backend/app/services/file_service.py:upload_files_to_agent_env()`
- Gets environment adapter via `DockerAdapter`
- For each file: reads from disk, sends multipart POST to `/files/upload`
- Returns mapping `{file_id: agent_env_path}`

**Adapter Method:** `backend/app/services/adapters/docker_adapter.py:upload_file_to_agent_env()`
- Async HTTP POST with file content
- Returns relative path from agent-env response

## File Locations Reference

**Backend:**
- Routes: `backend/app/api/routes/files.py`
- Services: `backend/app/services/file_service.py`, `file_storage_service.py`, `garbage_collection_service.py`, `file_cleanup_scheduler.py`
- Models: `backend/app/models/file_upload.py`, `session.py` (updated)
- Adapter: `backend/app/services/adapters/base.py`, `docker_adapter.py` (updated)
- Migration: `backend/app/alembic/versions/8510f306b385_add_file_uploads_table.py`
- Config: `backend/app/core/config.py` (added upload settings)

**Frontend:**
- Components: `frontend/src/components/Chat/FileUploadModal.tsx`, `FileBadge.tsx`, `MessageInput.tsx` (updated), `MessageBubble.tsx` (updated)
- Hooks: `frontend/src/hooks/useMessageStream.ts` (updated)
- Client: Auto-generated from OpenAPI (`frontend/src/client/*`)

**Agent-Env:**
- Routes: `backend/app/env-templates/python-env-advanced/app/core/server/routes.py` (added `/files/upload`)
- Service: `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py` (added file methods)
- Models: `backend/app/env-templates/python-env-advanced/app/core/server/models.py` (added `FileUploadResponse`)
- Prompts: `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py` (added environment context)

**Infrastructure:**
- `.gitignore` - Added `backend/data/uploads/` exclusion
- `docker-compose.yml` - Volume mount for uploads directory

## File Viewing Feature

Enables users to view files directly in the browser by clicking on them in the environment panel. Files open in a new browser tab with appropriate rendering based on file type.

**Flow:**
1. User clicks on supported file (e.g., CSV) in environment panel → opens new browser tab
2. Backend streams file content from agent environment workspace
3. Frontend renders file with appropriate viewer component
4. User can download file from viewer header

### Backend Implementation

**Workspace Routes:** `backend/app/api/routes/workspace.py`
- `GET /api/v1/environments/{env_id}/workspace/view-file/{path}` - Stream file content as text for rendering

### Frontend Implementation

**Route:** `frontend/src/routes/_layout/environment/$envId/file.tsx`
- Accepts `envId` (URL param) and `path` (search param)
- Opens in new browser tab via `window.open()`

**Components:**
- **FileViewer:** `frontend/src/components/Environment/FileViewer.tsx` - Main viewer with header, filename, path, and download button
- **CSVViewer:** `frontend/src/components/Environment/CSVViewer.tsx` - Renders CSV as table with proper parsing (handles quoted fields, escapes)
- **MarkdownViewer:** `frontend/src/components/Environment/MarkdownViewer.tsx` - Renders Markdown with GFM support and code syntax highlighting
- **TreeItemRenderer:** `frontend/src/components/Environment/TreeItemRenderer.tsx` - Updated to make viewable files (CSV, Markdown) clickable, opens in new tab

**File Type Renderers:**
Different file types are handled by specific viewer components located in `frontend/src/components/Environment/`:
- CSV files: `CSVViewer.tsx` (table rendering with headers)
- Markdown files: `MarkdownViewer.tsx` (rendered markdown with GFM support, syntax highlighting for code blocks)
- Additional file type renderers can be added alongside existing viewers

**Integration:**
- Environment panel file tree detects file types
- Clickable files (CSV, Markdown) have cursor pointer on hover
- Click handler opens new tab with file viewer route
- Download button remains independent from view action

## Remote Database Viewer

For viewing and querying SQLite databases generated by agents in the workspace, see **[remote_database_viewer.md](./remote_database_viewer.md)**.

This feature allows users to:
- Browse database schema (tables, views, columns) in a sidebar
- Query data with Auto mode (pagination) or Manual mode (custom SQL)
- Export query results as CSV

SQLite files (`.db`, `.sqlite`, `.sqlite3`) are clickable in the environment panel and open a dedicated database viewer in a new tab.

---

**Document Version:** 2.3 (Added Remote Database Viewer reference)
**Last Updated:** 2026-01-10
**Status:** Features Fully Implemented
