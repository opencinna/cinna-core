# Agent Message Attachments — Technical Details

## File Locations

### Backend

- **Models:** `backend/app/models/files/file_upload.py` — `FileUpload` (`origin`, `session_id` added), `MessageFile` (`source`, `event_seq` added), `FileUploadPublic` (`source` field added)
- **Migration:** `backend/app/alembic/versions/a7f3c9e2b1d4_add_agent_message_attachments.py`
- **Service:** `backend/app/services/files/attachment_materialization_service.py` — `AttachmentMaterializationService`, `MaterializationResult`, `MaterializedAttachment`
- **Service (extended):** `backend/app/services/sessions/message_service.py` — `_extract_attachments()`, `_coalesce_assistant_events()`, `_emit_attachment_event()`, `_emit_attachment_error_event()`, `MessageService._process_attachments()` (returns `live_attachment_events`; finalize block yields them)
- **Service (extended):** `backend/app/services/sessions/stream_event_handlers.py` — `WebSocketEventHandler.on_event()` skips `attachment` events (web double-render guard for the live-yield path)
- **Service (extended):** `backend/app/services/a2a/a2a_event_mapper.py` — `CONTENT_KIND_FILE`, `FILE_ID_KEY`, `FILE_NAME_KEY`, `FILE_MIME_KEY`, `FILE_SIZE_KEY`, `_build_attachment_file_part()`, `_build_file_download_uri()`, `create_file_status_update()`, `attachment` branch in `map_stream_event()` and `_build_parts_for_session_message()`
- **Service (extended):** `backend/app/services/environments/agent_workspace_token_service.py` — `create_file_download_token()`, `verify_file_download_token()`
- **Route (extended):** `backend/app/api/routes/files.py` — `GET /api/v1/files/{file_id}/download` — `?token=` and `?disposition=inline` parameters added

### Agent-Env (inside Docker container)

- **Prompt:** `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `_get_environment_context()` includes a compact "Attaching files to your reply" section (the only place the `<cinna_attach>` convention is taught to the agent — no separate always-loaded reference file). Includes a **"When to use it"** nudge: whenever the user asks to provide / generate / send / give / make / export / create a file, the agent should finish by emitting a `<cinna_attach>` tag rather than pasting the contents inline (added because some SDK models, notably OpenCode, otherwise end the turn after writing the file without attaching)

### Frontend

- **Component (new):** `frontend/src/components/Chat/AttachmentBlock.tsx` — card rendering for `attachment` events; amber error notice for `attachment_error` events; compact one-liner mode
- **Component (new):** `frontend/src/components/Chat/AttachmentPreviewModal.tsx` — in-place preview modal (image, PDF, CSV, Markdown, JSON, text); authenticated blob fetch with object-URL lifecycle management
- **Component (extended):** `frontend/src/components/Chat/StreamEventRenderer.tsx` — `attachment` and `attachment_error` branches added
- **Component (extended):** `frontend/src/components/Chat/MessageBubble.tsx` — agent-attachment fallback rendering via `message.files` (suppressed when inline `attachment` events are present); `FileBadge` wired with `onPreview` for agent attachments; `AttachmentPreviewModal` for the fallback path
- **Component (extended):** `frontend/src/components/Chat/FileBadge.tsx` — `onPreview` optional callback; `source === "agent_attachment"` detection
- **Component (extended):** `frontend/src/components/Webapp/WebappChatWidget.tsx` — `attachment_error` branch added to the stream event handler
- **Hook (extended):** `frontend/src/hooks/useSessionStreaming.ts` — `StreamEvent.type` union includes `"attachment"` and `"attachment_error"`; `metadata` interface extended with `file_id`, `filename`, `mime_type`, `size`, `agent_env_path`
- **Generated client:** `frontend/src/client/` — `FileUploadPublic` now includes `source` field; `?token=` and `?disposition=` query params surface in the SDK

## Database Schema

**Migration:** `backend/app/alembic/versions/a7f3c9e2b1d4_add_agent_message_attachments.py`

### `file_uploads` table (modified)

New columns:

| Column | Type | Constraint | Default | Purpose |
|--------|------|-----------|---------|---------|
| `origin` | VARCHAR(31) | NOT NULL | `'user'` | `'user'` (uploaded by a person) or `'agent'` (produced by an agent in its workspace) |
| `session_id` | UUID, FK → `session.id` ON DELETE SET NULL | nullable | NULL | Session the agent attachment was produced in; NULL for user uploads |

New index: `ix_file_uploads_session_id` (btree, non-unique)

### `message_files` table (modified)

New columns:

| Column | Type | Constraint | Default | Purpose |
|--------|------|-----------|---------|---------|
| `source` | VARCHAR(31) | NOT NULL | `'user_upload'` | `'user_upload'` or `'agent_attachment'` — drives rendering and A2A part kind |
| `event_seq` | INTEGER | nullable | NULL | Position of the corresponding `attachment` event in the message's `streaming_events`; used for inline ordering; NULL for user uploads |

`agent_env_path` (existing column) is reused to store the originating absolute workspace path for agent attachments (e.g. `/app/workspace/files/report.pdf`). The display filename is derived from its basename.

### Public schema changes

`FileUploadPublic` (`backend/app/models/files/file_upload.py`) adds:

```python
source: str = "user_upload"  # "user_upload" | "agent_attachment"
```

This field is populated from the joining `message_files.source` row when files are projected per-message into `MessagePublic.files`.

### `attachment` streaming event structure

Stored inside `message_metadata.streaming_events` (existing JSON list), alongside `assistant`, `tool`, `thinking`, `tool_result_delta`, and `webapp_action` events:

```json
{
  "type": "attachment",
  "content": "report.pdf",
  "event_seq": 6,
  "metadata": {
    "file_id": "f1e2d3c4-...",
    "filename": "report.pdf",
    "mime_type": "application/pdf",
    "size": 184223,
    "agent_env_path": "/app/workspace/files/report.pdf"
  }
}
```

`attachment_error` events (no record created):

```json
{
  "type": "attachment_error",
  "content": "attachment rejected: file type not allowed (text/html)",
  "event_seq": null
}
```

## API Routes

### `GET /api/v1/files/{file_id}/download` (extended)

**File:** `backend/app/api/routes/files.py`

Existing endpoint extended with two query parameters:

| Parameter | Type | Default | Behaviour |
|-----------|------|---------|-----------|
| `token` | `str \| None` | None | Signed `file_download` JWT (alternative to session JWT); verified via `AgentWorkspaceTokenService.verify_file_download_token()`; `token.file_id` must match the path `file_id`; expired → 401 |
| `disposition` | `str \| None` | None | `"inline"` serves `Content-Disposition: inline` for browser preview; only honoured for the inline-safe MIME set; all other types forced to `attachment` |

Auth precedence: `?token=` is checked first; if absent, falls back to the session-JWT `CurrentUserOrGuest` path.

Security headers always present: `X-Content-Type-Options: nosniff`. Inline responses additionally carry `Content-Security-Policy: default-src 'none'; sandbox`.

Inline-safe MIME types: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `application/pdf`, `text/plain`, `text/csv`, `text/markdown`, `application/json`. `text/html` and `image/svg+xml` are always forced to attachment.

No route signature change. Session messages continue to carry agent attachment files in `MessagePublic.files` (`GET /api/v1/sessions/{id}/messages`).

## Services

### AttachmentMaterializationService

**File:** `backend/app/services/files/attachment_materialization_service.py`

Module-level constants:

- `WORKSPACE_ROOT = "/app/workspace"` — the boundary all agent paths must be inside
- `MAX_ATTACHMENTS_PER_MESSAGE = 10`
- `MAX_AGGREGATE_BYTES_PER_MESSAGE = 100 * 1024 * 1024` (100MB)
- `_EXTENSION_MIME_FALLBACK` — extension→MIME map for types `mimetypes.guess_type` mishandles (`.md`, `.csv`, `.json`, `.txt`, `.log`, `.xml`, `.pdf`)

| Method | Signature | Description |
|--------|-----------|-------------|
| `materialize_attachments` | `async (db, session, message, paths) -> MaterializationResult` | Orchestrates the full per-message materialisation; resolves adapter once; tracks running storage usage; de-dupes by resolved-relative-path; returns `MaterializationResult` with `by_path` dict and `rejections` list; never raises for per-path failures |
| `_to_workspace_relative` | `(abs_path) -> str \| None` | Normalises `..`/`.` via `os.path.normpath` (no filesystem stat); asserts result starts with `WORKSPACE_ROOT + "/"`; returns workspace-relative remainder or None |
| `_pull_workspace_bytes` | `async (adapter, relative_path) -> tuple[bytes, str] \| None` | Docker adapters: try `adapter.get_local_workspace_file_path(relative)` → `Path.read_bytes()` first (fast host read of the **main** workspace volume). If the host path is missing (e.g. a separately-mounted sub-volume — see **App Data sub-volume**) or the read fails, **fall back** to `adapter.fetch_workspace_item_with_meta(relative)` (in-container HTTP, sees every mount). Remote adapters use the HTTP path directly (retries once on generic error; `ValueError` from env-core boundary check → immediate None). MIME is always sniffed server-side from the extension, not from transport headers |
| `_sniff_mime` | `(path) -> str` | `mimetypes.guess_type()` then `_EXTENSION_MIME_FALLBACK` then `application/octet-stream` |
| `_validate` | `(size, mime, aggregate_bytes, running_usage) -> str \| None` | MIME whitelist, per-file size cap, per-message aggregate cap, owner quota; returns rejection reason string or None |
| `_store_and_record` | `(db, owner_id, session_id, message_id, abs_path, content, mime_type) -> MaterializedAttachment` | Derives display filename from `os.path.basename(abs_path)` → `FileStorageService.sanitize_filename()`; creates `FileUpload(origin="agent", status="attached")` + `MessageFile(source="agent_attachment")`; commits and refreshes |
| `_get_adapter` | `(db, session) -> adapter \| None` | Resolves `AgentEnvironment` and returns lifecycle adapter; returns None on any failure |

#### App Data sub-volume (Docker host-read fallback)

`/app/workspace/app-data` is a **separate Docker bind mount** (the per-user App Data volume, host path `data/agents/app-data/<user>/<bundle>/…`), not part of the env's workspace volume. The Docker host-read fast path (`get_local_workspace_file_path`) joins the relative path under the *workspace* host dir, so files under `app-data/` are absent there and the host read returns `None`. Without the fallback, materialisation **rejects** the file (no `attachment` event, no `MessageFile`; the rejection is emitted live only and the tag-only assistant event is stripped to empty), so the agent gets no badge even though the file exists. The fallback to `fetch_workspace_item_with_meta` reads inside the container — where `app-data` is correctly mounted — mirroring how `AgentStatusService` reads `app-data/storage/STATUS.md`. Regression: `backend/tests/unit/test_attachment_pull_workspace_bytes.py`.

### MessageService (extended)

**File:** `backend/app/services/sessions/message_service.py`

Module-level regex:

```python
_ATTACH_TAG_RE = re.compile(r"<cinna_attach>(.*?)</cinna_attach>", re.DOTALL)
```

| Method / function | Description |
|-------------------|-------------|
| `_extract_attachments(content)` | Returns `(paths, cleaned_content)`; trimmed non-empty absolute-path bodies collected in textual order; all tags stripped from cleaned_content regardless of validity (mirrors `_extract_webapp_actions`) |
| `_emit_attachment_event(session_id, event_seq, event_meta)` | Emits `attachment` WebSocket stream event to `session_{id}_stream`; failure-isolated |
| `_emit_attachment_error_event(session_id, reason)` | Emits `attachment_error` WebSocket stream event; failure-isolated |
| `_coalesce_assistant_events(events)` | Merges runs of consecutive `assistant` events into one (stops at any other type), renumbers `event_seq` contiguously. Runs first in the finalize block so OpenCode per-newline assistant fragments don't shatter multi-line markdown. See [multi_sdk_tech.md](../agent_environment_core/multi_sdk_tech.md). |
| `MessageService._process_attachments(session_id, agent_message_id, streaming_events, agent_content, get_fresh_db_session)` | Called from the `stream_message_with_events` finalize pass when `_ATTACH_TAG_RE.search(agent_content)` is true; runs materialisation off-loop via `asyncio.to_thread`; splices `attachment` events into the streaming trace at tag positions; renumbers `event_seq` contiguously; emits live WS events; persists `event_seq` on `MessageFile` rows; returns `(new_streaming_events, cleaned_agent_content, live_attachment_events)` — the 3rd element is the list of `attachment` event dicts the generator must yield (see **Live A2A delivery**) |

Finalize-pass call order in `stream_message_with_events()`:

1. `_coalesce_assistant_events()` — merge consecutive `assistant` fragments
2. `<webapp_action>` post-processing (existing)
3. `_ATTACH_TAG_RE.search(agent_content)` guard → `_process_attachments()`, then **yield each returned `live_attachment_events` item** into the stream (after the assistant text events, before `done`)
4. `streaming_events` written into `response_metadata`
5. `_finalize_agent_message()` persists the message

#### Live A2A delivery (attachment FilePart on the streaming SSE)

Materialised `attachment` events reach clients on **two channels**, and both are needed:

- **Socket.IO (web)** — `_emit_attachment_event()` publishes to the `session_{id}_stream` room. Fires regardless of which client drives the stream, so web watchers of an A2A-driven session still see the file.
- **Stream generator (A2A live)** — `_process_attachments` also returns the events as `live_attachment_events`, and `stream_message_with_events` yields each into the generator. `SessionStreamProcessor` forwards them to `A2AStreamEventHandler` → `A2AEventMapper.map_stream_event()` → a `working`/non-final FilePart status-update. Streaming A2A clients (Cinna Desktop/Mobile) consume the live SSE and persist their own copy (they don't replay via GetTask), so without this yield they never receive the FilePart — the mapper's `attachment` branch was dead on the live path.

To avoid a web double-render, `WebSocketEventHandler.on_event()` in `backend/app/services/sessions/stream_event_handlers.py` **skips** `type=="attachment"` (web is already covered by the explicit Socket.IO emit). `MCPEventHandler` ignores unknown types (no-op). Ordering caveat: the live A2A FilePart lands at the END of the turn (yielded at finalize), whereas the persisted trace / GetTask replay splices it inline at the tag position. `attachment_error` events remain Socket.IO-only. Regression: `test_a2a_attachment_emits_file_part_on_live_stream` in `backend/tests/api/agents/sessions/agents_message_attachments_test.py` (verified to fail if the yield is removed).

### A2AEventMapper (extended)

**File:** `backend/app/services/a2a/a2a_event_mapper.py`

New module-level constants:

| Constant | Value | Use |
|----------|-------|-----|
| `CONTENT_KIND_FILE` | `"file"` | `cinna.content_kind` value for attachment parts |
| `FILE_ID_KEY` | `"cinna.file_id"` | Part metadata: platform file UUID |
| `FILE_NAME_KEY` | `"cinna.file_name"` | Part metadata: display filename |
| `FILE_MIME_KEY` | `"cinna.file_mime"` | Part metadata: sniffed MIME type |
| `FILE_SIZE_KEY` | `"cinna.file_size"` | Part metadata: file size in bytes |

The `_STREAM_EVENT_TO_CONTENT_KIND` dict now includes `"attachment": CONTENT_KIND_FILE`.

New functions:

| Function | Description |
|----------|-------------|
| `_build_file_download_uri(file_id, session_id)` | Builds `{FRONTEND_HOST}{API_V1_STR}/files/{file_id}/download?token=<signed>` using `AgentWorkspaceTokenService.create_file_download_token()` |
| `_build_attachment_file_part(event_meta, session_id)` | Returns a `Part(root=FilePart(file=FileWithUri(...), metadata={CONTENT_KIND_KEY: CONTENT_KIND_FILE, FILE_ID_KEY, FILE_NAME_KEY, FILE_MIME_KEY, FILE_SIZE_KEY}))` or None if `file_id` is absent; metadata on part, never on Message |

`create_file_status_update(task_id, context_id, state, final, file_part)` — new factory; builds a `TaskStatusUpdateEvent` whose message carries a single `FilePart`; used by the `attachment` branch in `map_stream_event()`.

Extended methods:

- `map_stream_event()` — `attachment` event type → `create_file_status_update()` with a `working` / non-final status so streaming A2A clients see the file as it is finalised. This branch is exercised live because the finalize block now **yields** the `attachment` events into the generator (see **Live A2A delivery** in the MessageService section)
- `_build_parts_for_session_message()` — `attachment` events in the stored trace produce `FilePart` items (via `_build_attachment_file_part`) rather than `TextPart` items; signed URLs are regenerated at replay time, so they remain valid

### AgentWorkspaceTokenService (extended)

**File:** `backend/app/services/environments/agent_workspace_token_service.py`

| Method | Signature | Description |
|--------|-----------|-------------|
| `create_file_download_token` | `(file_id: UUID, session_id: UUID, expiry: timedelta = 1h) -> str` | Mints a JWT with `{"type": "file_download", "file_id": str, "session_id": str, "exp": ...}`; signed with `settings.SECRET_KEY` / `ALGORITHM` |
| `verify_file_download_token` | `(token: str) -> dict \| None` | Decodes and verifies; checks `type == "file_download"` and both claim fields present; returns `{"file_id": str, "session_id": str}` or None; expired → None (not raised) |

Token validity: `FILE_DOWNLOAD_TOKEN_EXPIRY = timedelta(hours=1)` (module constant).

## Frontend Components

### AttachmentBlock.tsx

**File:** `frontend/src/components/Chat/AttachmentBlock.tsx`

Props:

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `variant` | `"attachment" \| "attachment_error"` | `"attachment"` | Switches between file card and error notice |
| `fileId` | `string \| undefined` | — | Platform file UUID |
| `filename` | `string \| undefined` | — | Display filename |
| `mimeType` | `string` | `""` | Drives icon selection |
| `size` | `number \| undefined` | — | Bytes for size display |
| `errorReason` | `string \| undefined` | — | Failure text for `attachment_error` variant |
| `isCompact` | `boolean \| undefined` | false | Renders compact one-liner instead of full card |

Full card: bordered block with MIME icon, truncated filename (≤28 chars, extension preserved), optional size, and an explicit Download button; clicking the card body opens `AttachmentPreviewModal`. Error variant: amber left-bordered notice with `AlertTriangle` icon.

Download: authenticated fetch to `GET /api/v1/files/{fileId}/download` with `Authorization: Bearer <localStorage.access_token>`; creates a temporary anchor and revokes the object URL after click.

### AttachmentPreviewModal.tsx

**File:** `frontend/src/components/Chat/AttachmentPreviewModal.tsx`

`classifyMime()` maps MIME types to one of: `image`, `pdf`, `csv`, `markdown`, `json`, `text`, `none`.

Preview fetch: `GET /api/v1/files/{fileId}/download?disposition=inline` with session JWT. Binary previews (image, pdf) use object URLs revoked on modal close / unmount. Text-based previews (csv, markdown, json, text) use `blob.text()`. MIME kind `none` shows "no preview available" with a Download button.

Viewers reused from `frontend/src/components/Environment/`:

- `image/*` → `<img>` with `alt={filename}`
- `application/pdf` → `<iframe>`
- `text/csv` → `CSVViewer`
- `text/markdown` / `text/x-markdown` → `MarkdownViewer`
- `application/json` → `JSONViewer`
- other `text/*` → `TextViewer`

Error state: "Couldn't load preview — try downloading." with Download button.

### StreamEventRenderer.tsx (extended)

**File:** `frontend/src/components/Chat/StreamEventRenderer.tsx`

New branches:

```
event.type === "attachment"       → AttachmentBlock(variant="attachment", fileId, filename, mimeType, size, isCompact)
event.type === "attachment_error" → AttachmentBlock(variant="attachment_error", errorReason=event.content)
```

### MessageBubble.tsx (extended)

**File:** `frontend/src/components/Chat/MessageBubble.tsx`

File rendering logic:

```
message.files
  userFiles  = files where source !== "agent_attachment"  → always rendered as downloadable FileBadge
  agentFiles = files where source === "agent_attachment"
  hasAttachmentEvents = streaming_events.some(e => e.type === "attachment")
  fallbackAgentFiles  = hasAttachmentEvents ? [] : agentFiles   ← suppressed when inline events present
  badges = [...userFiles, ...fallbackAgentFiles]
```

`FileBadge` for agent attachments: `downloadable={false}`, `onPreview={(f) => setPreviewFile(f)}`. `AttachmentPreviewModal` is rendered at the bottom of the component for the fallback path.

### FileBadge.tsx (extended)

**File:** `frontend/src/components/Chat/FileBadge.tsx`

Added optional `onPreview?: (file: FileUploadPublic) => void` prop. When present, clicking the badge body calls `onPreview` instead of triggering a download. `isAgentAttachment = file.source === "agent_attachment"` adds a visible `border border-border` ring to distinguish agent-produced files.

### useSessionStreaming.ts (extended)

**File:** `frontend/src/hooks/useSessionStreaming.ts`

`StreamEvent.type` union extended: `"attachment" | "attachment_error"` added.

`StreamEvent.metadata` interface extended:

```typescript
file_id?: string
filename?: string
mime_type?: string
size?: number
agent_env_path?: string
```

`attachment_error` events carry no `event_seq` and are appended to the current events list without dedup/gap treatment (handled by a dedicated branch in the stream handler).

### WebappChatWidget.tsx (extended)

**File:** `frontend/src/components/Webapp/WebappChatWidget.tsx`

`attachment_error` branch added to the stream event handler, appending an error notice event to the chat for embedded webapp chats.

## Configuration

No new environment variables. Reuses existing settings from `backend/app/core/config.py`:

- `allowed_mime_types` — combined whitelist from `UPLOAD_ALLOWED_MIME_TYPES`
- `upload_max_file_size_bytes` — per-file cap (default 100MB)
- `upload_max_user_storage_bytes` — owner quota (default 10GB)
- `UPLOAD_MAX_FILE_SIZE_MB` — used in human-readable rejection messages
- `FRONTEND_HOST` + `API_V1_STR` — base for A2A signed download URLs
- `SECRET_KEY` + `ALGORITHM` — JWT signing for `file_download` tokens

## A2A Part Contract Reference

A `FilePart` for an agent attachment carries the following part-level metadata (on `FilePart.metadata`, never on `Message.metadata`):

| Key | Type | Description |
|-----|------|-------------|
| `cinna.content_kind` | `"file"` | Part discriminator — identifies this as a file attachment (not text/thinking/tool) |
| `cinna.file_id` | `string` | Platform file UUID; use to construct a fresh download URL if the embedded one expires |
| `cinna.file_name` | `string` | Display filename (path basename) |
| `cinna.file_mime` | `string` | Sniffed MIME type |
| `cinna.file_size` | `integer` | File size in bytes |

`FileWithUri.uri` format: `{FRONTEND_HOST}/api/v1/files/{file_id}/download?token=<signed>` — valid 1 hour from the time the message was finalised (or replayed).

---

*Last updated: 2026-06-06*
