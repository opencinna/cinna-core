# File Sending & UI

## Purpose

Users can attach files to chat messages to share documents, images, and other data with agents. Files are uploaded to the platform, transferred into the agent's Docker environment, and referenced in the message so the agent can read them. In the chat UI, attached files appear as compact visual badges with download capability.

## Core Concepts

- **File Upload** — Files are uploaded to the platform storage first (via `FilesService`), returning a `FileUploadPublic` record with a temporary status. Files remain temporary until attached to a message
- **File Attachment** — When a message is sent with file IDs, the backend uploads files into the agent environment, creates `MessageFile` junction records, and marks files as "attached"
- **File Badge** — Compact visual indicator showing filename, icon (by MIME type), and file size. Badges on sent messages are clickable for download; badges on pending messages have a remove button
- **Agent-Env Path** — When files are uploaded to the agent environment, the container path is stored in `MessageFile.agent_env_path` so the agent knows where to find the file
- **Message Content Augmentation** — The agent receives an augmented version of the user's message with file paths prepended, while the original content is stored in the database for display

## User Flow

### Attaching via Upload Modal

1. User clicks the "+" button next to the message textarea
2. Selects "Attach File" from the dropdown menu
3. FileUploadModal opens with drag-drop zone
4. User drags files or clicks to browse (100MB max per file, multiple files allowed)
5. Each file uploads immediately via `FilesService.uploadFile()`
6. On success, file appears as a FileBadge below the textarea
7. User can remove attached files before sending (X button on badge, triggers API delete)
8. When message is sent, file IDs are included in the request

### Attaching via Drag-Drop on Textarea

1. User drags file(s) over the message textarea area
2. Drag overlay appears: blue border + "Drop files to attach" text
3. User drops files → each file validates size (100MB) and uploads
4. Files appear as badges below the textarea
5. Same send flow as above

### Viewing Files in Messages

1. Sent messages with files show FileBadge components below the message content
2. Each badge shows: file-type icon (image/text/archive/generic), truncated filename, file size on hover
3. Clicking a badge triggers authenticated download via `GET /api/v1/files/{id}/download`
4. Download creates a blob URL and triggers browser save dialog

## File Lifecycle

```
User selects file
    → POST /api/v1/files/upload (multipart/form-data)
    → FileUpload record created (status: "temporary")
    → File stored at uploads/{user_id}/{file_id}/{filename}
    → FileBadge shown in input area (removable)

User sends message with file IDs
    → Backend validates: ownership, status="temporary"
    → Files uploaded to agent-env container via FileService
    → Agent-env paths stored in MessageFile.agent_env_path
    → User message created (original content)
    → Agent receives augmented content: "Uploaded files:\n- /path/to/file\n---\n\n{original}"
    → Files marked as "attached" (status update)

Files displayed in message
    → message.files[] populated when loading messages
    → FileBadge rendered with downloadable=true
    → Click triggers authenticated blob download
```

## Business Rules

- File size limit: 100MB per file (validated client-side and server-side)
- Multiple files can be attached to a single message
- Files must be owned by the sending user (validated on attach)
- Files must have `status="temporary"` — already-attached files cannot be re-attached
- Temporary files that are never attached are subject to garbage collection
- Removing a file before sending calls `DELETE /api/v1/files/{id}` (permanent deletion)
- File badges are read-only in received messages — only download action available
- File upload is only available in the session page context — not in guest share input or webapp widget input
- The agent sees file paths prepended to the message content, but the user sees only their original text
- Download requires authentication via JWT token in Authorization header

## File Type Icons

| MIME Type Pattern | Icon |
|------------------|------|
| `image/*` | Image icon |
| `text/*` | FileText icon |
| `*zip*`, `*tar*` | Archive icon |
| Everything else | Generic File icon |

## Filename Display

- Filenames truncated to 20 characters with extension preserved
- Format: `first_chars...ext` (e.g., `very_long_docum...pdf`)
- Full filename and size shown on badge hover tooltip

## Integration Points

- **[Chat Windows](chat_windows.md)** — File badges render inside MessageBubble, FileUploadModal triggered from MessageInput
- **[Agent File Management](../../agents/agent_file_management/agent_file_management.md)** — Backend file upload/download service, agent-env file transfer, storage management
