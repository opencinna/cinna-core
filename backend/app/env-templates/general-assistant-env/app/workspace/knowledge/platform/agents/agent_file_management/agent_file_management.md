# Agent File Management

## Purpose

Enables users to upload files to agent chat sessions, view workspace files in the browser, and download files from message history. Files are stored in backend storage and transferred to agent Docker environments where agents access them at `./uploads/`.

## Core Concepts

- **File Upload** - Attaching files to chat messages for agent processing
- **File Lifecycle** - States a file moves through: temporary → attached → marked for deletion
- **Backend Storage** - Persistent file storage on the backend server (`backend/data/uploads/{user_id}/{file_id}/`)
- **Agent-Env Transfer** - Copying files from backend storage into the agent's Docker workspace (`/app/workspace/uploads/`)
- **File Viewing** - Opening workspace files (CSV, Markdown, JSON, text) in a browser tab
- **Storage Quota** - Per-user limit on total file storage (default 10GB)

## User Stories / Flows

### File Upload Flow

1. User clicks paperclip icon in chat message input
2. File upload modal opens with drag-and-drop zone
3. User selects/drops files → files upload to backend as **temporary**
4. User sends message with attached files
5. Backend transfers files to agent environment workspace (`/app/workspace/uploads/`)
6. Agent receives message with file paths prepended (e.g., `./uploads/document.pdf`)
7. Agent processes files in isolated workspace

### File Download Flow

1. User sees file badges on messages with attachments
2. User clicks download button on a file badge
3. Backend streams file content with auth check (owner or session participant)

### File Viewing Flow

1. User clicks on a supported file (CSV, Markdown, JSON, TXT, LOG) in the environment panel
2. File opens in a new browser tab
3. Backend streams file content from agent environment workspace
4. Frontend renders with appropriate viewer (table for CSV, rendered markdown, collapsible tree for JSON, monospace for text)
5. User can download file from the viewer header

### File Deletion Flow

1. User deletes a temporary (unattached) file from upload modal
2. File is soft-deleted (marked for deletion)
3. Garbage collection removes files marked for deletion >24h ago (runs daily at 3 AM)

## Business Rules

### File Lifecycle States

- **temporary** - Uploaded but not attached to a message; can be deleted by the user
- **attached** - Associated with a sent message; permanent (cannot be deleted by user)
- **marked_for_deletion** - Soft deleted; garbage collected after 24 hours

### Validation Rules

- Maximum file size: 100MB per file (configurable)
- Per-user storage quota: 10GB (configurable)
- MIME type whitelist: PDF, CSV, images, code files, and other allowed types
- Filename sanitization: dangerous characters stripped, unicode normalized, directory traversal prevented

### Access Control

- **Upload**: Authenticated users and guest share users
- **Download**: File owner OR session owner
- **Delete**: File owner only
- **View**: Same as download permissions
- Guest users: files are attributed to the agent owner for ownership and quota purposes

### Supported File Viewers

- **CSV**: Table rendering with proper parsing (quoted fields, escapes)
- **Markdown**: Rendered with GFM support and code syntax highlighting
- **JSON**: Collapsible tree with type-aware color coding, expand/collapse toolbar
- **Text/Log**: Preformatted monospace text with word wrapping

### `/files` Session Command

Users can type `/files` in any agent session to get a markdown listing of all workspace files grouped by section (Files, Scripts, Logs, Docs, Uploads). Each file is a clickable link that opens the appropriate viewer. For A2A clients, links use short-lived tokens (1-hour JWTs) for browser access without regular auth.

## Architecture Overview

```
User → Frontend UI → Backend API → Backend Storage (disk + DB)
                                         ↓
                          Agent-Env Upload (Docker API) → Agent Access (./uploads/)
```

**Storage locations:**
- Backend disk: `backend/data/uploads/{user_id}/{file_id}/`
- Database: `file_uploads` + `message_files` tables (metadata)
- Agent environment: `/app/workspace/uploads/` (transferred copies)

**File viewing flow:**
```
Environment Panel → Frontend Route (new tab) → Backend Workspace API → Agent-Env → File Content
```

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** - File upload is part of the message send flow; files are attached to messages
- **Agent Environments** - Files are transferred to Docker workspace via adapter; agent-env has upload endpoint. See [Agent Environments](../agent_environments/agent_environments.md)
- **Agent Environment Core** - Agent-env server handles file upload reception, filename sanitization, workspace tree updates, and prompt generation for uploaded files. See [Agent Environment Core](../agent_environment_core/agent_environment_core.md)
- **Guest Sessions** - File endpoints support guest access; guest file viewer uses a standalone route <!-- TODO: create guest_sessions docs -->
- **Agent Commands** - `/files` command lists workspace files with clickable viewer links <!-- TODO: create agent_commands docs -->
- **Remote Database Viewer** - SQLite files in workspace open a specialized database viewer. See [Remote Database Viewer](./remote_database_viewer.md)

---

*Last updated: 2026-03-02*
