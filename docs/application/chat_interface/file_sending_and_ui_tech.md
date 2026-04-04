# File Sending & UI — Technical Reference

## File Locations

### Frontend

- `frontend/src/components/Chat/MessageInput.tsx` — File attachment orchestration: "+" dropdown with "Attach File" option, drag-drop handlers on textarea wrapper, `attachedFiles` state array, upload via `FilesService.uploadFile()`, delete via `FilesService.deleteFile()`, sends `fileIds` on message submit
- `frontend/src/components/Chat/FileUploadModal.tsx` — Dialog with `react-dropzone`: drag-drop zone, 100MB validation, upload mutation, success/error/loading states
- `frontend/src/components/Chat/FileBadge.tsx` — Badge component: MIME-type icon selection, filename truncation (20 chars), file size formatting, authenticated download via fetch + blob URL + programmatic anchor click, optional remove button (`onRemove` prop)
- `frontend/src/components/Chat/MessageBubble.tsx` — Renders `message.files[]` as `FileBadge` components with `downloadable=true` in a bordered footer section below message content
- `frontend/src/hooks/useSessionStreaming.ts` — `sendMessage()` accepts `fileIds?: string[]` and `fileObjects?: Array<{id, filename, file_size, mime_type}>`, includes `file_ids` in POST body, adds `fileObjects` to optimistic user message for instant badge display

### Backend — API Routes

- `backend/app/api/routes/files.py` — Three endpoints:
  - `POST /api/v1/files/upload` — `upload_file()`: multipart upload, creates FileUpload record with status "temporary"
  - `DELETE /api/v1/files/{file_id}` — `delete_file()`: deletes file record and storage
  - `GET /api/v1/files/{file_id}/download` — `download_file()`: returns file content with authentication
- `backend/app/api/routes/messages.py` — `send_message_stream()`: detects `file_ids` in request body, delegates to `MessageService.prepare_user_message_with_files()` when present

### Backend — Services

- `backend/app/services/sessions/message_service.py` — `prepare_user_message_with_files()`:
  1. Validates file ownership and "temporary" status
  2. Calls `FileService.upload_files_to_agent_env()` to transfer files into Docker container
  3. Creates user message with original content (no file paths in stored content)
  4. Creates `MessageFile` junction records with `agent_env_path`
  5. Marks files as "attached" via `FileService.mark_files_as_attached()`
  6. Returns `(user_message, augmented_content)` — augmented content has file paths prepended for agent
- `backend/app/services/files/file_service.py` — `upload_files_to_agent_env()`: transfers files from platform storage to agent environment container, returns `{file_id: container_path}` mapping

### Backend — Models

- `backend/app/models/files/file_upload.py` — Three models:
  - `FileUpload` (table) — `id`, `user_id`, `filename`, `file_path`, `file_size`, `mime_type`, `status` ("temporary"/"attached"/"marked_for_deletion"), timestamps, `file_metadata`
  - `MessageFile` (junction table) — `message_id`, `file_id`, `agent_env_path`
  - `FileUploadPublic` (response schema) — `id`, `filename`, `file_size`, `mime_type`, `status`, `uploaded_at`

## Data Flow

### Upload & Attach

```
MessageInput: user clicks "Attach File"
  → FileUploadModal opens
  → User drops file → FilesService.uploadFile({ formData: { file } })
  → POST /api/v1/files/upload → FileUpload record (status: "temporary")
  → onFileUploaded callback → attachedFiles state updated
  → FileBadge rendered below textarea (with onRemove)

MessageInput: user clicks Send
  → onSend(content, fileIds) called
  → useSessionStreaming.sendMessage(content, undefined, fileIds, fileObjects)
  → Optimistic message added to cache with files[] for instant badge display
  → POST /api/v1/sessions/{id}/messages/stream { content, file_ids }
  → Backend: prepare_user_message_with_files()
    → Validate ownership + status
    → FileService.upload_files_to_agent_env() → container paths
    → Create message + MessageFile records
    → Mark files as "attached"
    → Return augmented content for agent
```

### Download

```
FileBadge click (downloadable=true)
  → fetch(GET /api/v1/files/{id}/download, { Authorization: Bearer token })
  → Response → blob
  → URL.createObjectURL(blob) → anchor.click() → browser save dialog
  → URL.revokeObjectURL()
```

### Drag-Drop on Textarea

```
MessageInput div: onDragOver → setIsDraggingOver(true), visual overlay
  → onDrop → Array.from(e.dataTransfer.files)
  → forEach: validate size (100MB) → uploadMutation.mutate(file)
  → onSuccess → attachedFiles state updated → FileBadge rendered
  → onDragLeave → setIsDraggingOver(false)
```

## Optimistic File Display

When sending a message with files, `useSessionStreaming.ts:sendMessage()` creates an optimistic user message that includes file objects:

- `fileObjects` parameter provides `{id, filename, file_size, mime_type}` for each attached file
- These are set on the optimistic message's `files` array
- FileBadges render immediately from this data before the backend confirms
- On next message query refetch, the real message with server-populated files replaces the optimistic one

## Session Page Route Integration

`frontend/src/routes/_layout/session/$sessionId.tsx` supports initial file attachments via URL search params:
- `fileIds: string[]` — Pre-attached file IDs (from task execution or navigation)
- `fileObjects: Array<{id, filename, file_size, mime_type}>` — File metadata for immediate display
- These are passed to `sendMessage()` when the initial message auto-sends on page load

## Message Content: User vs Agent View

| Audience | Content |
|----------|---------|
| Database / UI | Original user text only (no file paths) |
| Agent environment | `"Uploaded files:\n- /path/to/file1\n- /path/to/file2\n---\n\n{original text}"` |

The augmented content is only used for the SSE payload to the agent environment. The stored `message.content` always contains the original text.
