# Agent Message Attachments

## Purpose

Agents can attach files they produce in their workspace directly to their reply messages. The platform materialises the file into durable storage, records it as part of the message's structured data, and renders it as a clickable inline card — with preview and download — everywhere messages are displayed: web chat, guest share, webapp chat widget, and A2A clients (Cinna Desktop / Mobile). Because attachments live in the message data structure they travel over every channel that already replays messages, with no extra integration work required.

This is the reverse of a user uploading a file to an agent. Both directions now work symmetrically.

## Core Concepts

- **`<cinna_attach>` tag** — agent-side text tag (`<cinna_attach>/app/workspace/files/report.pdf</cinna_attach>`) that declares a file to attach; mirrors the `<webapp_action>` convention; stripped from visible text at finalize time
- **Materialisation** — finalize-time backend process that pulls file bytes from the agent environment, validates them, stores them in durable backend storage, and creates `FileUpload` + `MessageFile` records
- **`attachment` streaming event** — the structured event spliced into the message's `streaming_events` trace that carries `file_id`, `filename`, `mime_type`, and `size`; drives both live chat rendering and A2A transport
- **`attachment_error` event** — a failure notice emitted when materialisation is rejected, without creating any record; the message itself always completes normally
- **Signed download token** — short-lived JWT (type `file_download`, 1 hour) embedded in A2A `FilePart` URIs so native clients can fetch files without a regular session JWT
- **Inline preview** — web app fetches files with `?disposition=inline`; only a known-safe MIME allowlist is served inline to prevent stored-XSS

## User Stories / Flows

### Viewing an Agent Attachment (Web Chat)

1. Agent completes a reply that includes one or more `<cinna_attach>` tags
2. Backend strips the tags from the visible text and materialises each referenced file
3. An `attachment` streaming event is emitted per file; the `AttachmentBlock` card appears inline at the position the tag was in the reply
4. User clicks the card body — `AttachmentPreviewModal` opens with an in-place preview (image, PDF, CSV, Markdown, JSON, or plain text)
5. User clicks the Download button on the card or inside the modal — authenticated blob download is triggered via the JWT path
6. If materialisation fails, a small amber error notice appears instead of a card; the rest of the reply is unaffected

### Viewing an Agent Attachment (Cinna Desktop / Mobile via A2A)

1. Agent reply finalises with `<cinna_attach>` tags
2. `A2AEventMapper` converts each `attachment` streaming event into a native `FilePart(FileWithUri)` carrying a 1-hour signed download URL and `cinna.content_kind="file"` metadata
3. The `FilePart` is delivered **on the live streaming SSE** (as a `working` status-update at the end of the turn) — not only on replay — so a streaming client that consumes the live stream and persists its own copy receives the file without a follow-up GetTask. Replay (GetTask / page reload) reconstructs the same `FilePart` from the persisted trace.
4. Native client reads the `FilePart`, renders a download (and optionally an inline preview)
5. If the signed URL has expired, the client re-requests the message (or A2A GetTask) to receive a fresh signed URL

### Viewing via `message.files` Fallback

For messages that were completed before the streaming trace is available (replays, page reloads, messages sent before the feature was deployed), agent attachments in `MessagePublic.files` with `source="agent_attachment"` render as preview-enabled `FileBadge` items below the message. When inline `attachment` events are already present in the streaming trace, the `message.files` fallback for agent attachments is suppressed to avoid duplicate rendering.

## Agent Instruction Convention

Agents declare attachments by embedding one or more `<cinna_attach>` tags anywhere in their reply:

```
Here is the quarterly report and the underlying data.
<cinna_attach>/app/workspace/files/q4-report.pdf</cinna_attach>
<cinna_attach>/app/workspace/app-data/storage/q4-data.csv</cinna_attach>
```

The agent is taught the convention via a compact section in its system prompt (`prompt_generator._get_environment_context()`). The full server-enforced rules are:

- The tag body must be an **absolute container path** rooted at `/app/workspace`; relative paths and any path outside the workspace root are rejected
- The file must already exist when the reply finalises; the agent must write the file before ending its turn
- No caption or name field — the display name is the on-disk filename (path basename), sanitised
- The tag position in the reply determines where the attachment card appears inline; the tag itself is stripped from visible text
- Constraints: allowed MIME types only; 100MB per file; max 10 attachments per message; 100MB aggregate per message

Affected existing installs need an environment rebuild to receive the updated prompt section (same as all prompt-generator changes).

## Business Rules

### Path and Security Validation

- Path must be absolute (start with `/`) and rooted at `/app/workspace`; `..` and `.` segments are normalised before the workspace-root boundary check; symlinks are not followed (normalisation only — no filesystem stat required)
- Any path that after normalisation does not start with `/app/workspace/` (with trailing slash) is rejected
- App Data (`/app/workspace/app-data/`) is inside the workspace root and is therefore attachable

### MIME Type and Size Rules

- MIME is sniffed server-side from the file extension using `mimetypes.guess_type`, with an explicit fallback map for documented previewable types that the standard library mishandles (`.md`, `.csv`, `.json`, `.txt`, `.log`, `.xml`, `.pdf`)
- The resulting MIME is checked against `settings.allowed_mime_types` (same whitelist used for user uploads); disallowed types emit `attachment_error` and are skipped
- Per-file size cap: `settings.upload_max_file_size_bytes` (default 100MB)
- Per-message aggregate cap: 100MB total across all attachments in one message
- Per-message count cap: 10 attachments

### Ownership and Quota

- Attached files are owned by (`user_id` = ) the session owner; for guest sessions this is the agent owner — consistent with the existing guest-upload attribution rule
- Owner storage quota is enforced before storing (`settings.upload_max_user_storage_bytes`, default 10GB); over-quota paths emit `attachment_error`

### Deduplication

- The same absolute path declared twice in one message stores the file once; both `attachment` events reference the same `file_id`

### Lifecycle

- Agent attachments are created directly as `status="attached"` (they are never removable by users as temporary uploads)
- `MessageFile` records cascade-delete when the message or session is deleted; orphaned `FileUpload` rows are reclaimed by the existing garbage-collection scheduler

### Download Access Control

- JWT path: file owner OR any session participant (same `FileService.check_download_permission` used for user uploads)
- Signed token path: a valid `file_download` JWT whose `file_id` claim matches the requested file; token expiry → 401

### Inline Preview Safety

`?disposition=inline` is honoured only for a known-safe MIME set (`image/png`, `image/jpeg`, `image/gif`, `image/webp`, `application/pdf`, `text/plain`, `text/csv`, `text/markdown`, `application/json`). Types that can execute script in the app origin (`text/html`, `image/svg+xml`) are forced to `Content-Disposition: attachment` regardless of the query parameter. All responses carry `X-Content-Type-Options: nosniff`; inline responses additionally carry `Content-Security-Policy: default-src 'none'; sandbox`.

### Materialisation Is Finalize-Only

Tags are only extracted and materialised after the agent stream completes (the finalize pass). A stream interrupted before completing produces no attachment records and no `attachment` events.

### Error Isolation

A failed or rejected attachment never fails the message. `attachment_error` events are emitted for each rejection reason; the agent reply text (minus the stripped tags) is always persisted and displayed.

## Integration Points

- **[Agent File Management](./agent_file_management.md)** — reuses `FileUpload`, `MessageFile`, `FileService`, `FileStorageService`, the `/files/{id}/download` endpoint, storage quota, and the MIME/size config; extends those models with `origin` and `session_id` (FileUpload) and `source` and `event_seq` (MessageFile)
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — attachment materialisation runs inside the `stream_message_with_events` finalize pass, which is part of the session streaming pipeline; `attachment` and `attachment_error` events join the existing `streaming_events` trace stored in `message_metadata`
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — bytes are pulled from the agent workspace via `DockerAdapter.get_local_workspace_file_path()` (Docker volume) or `adapter.fetch_workspace_item_with_meta()` (remote); the agent instruction convention is injected by `prompt_generator._get_environment_context()`
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — `A2AEventMapper` converts each `attachment` event into a native `FilePart(FileWithUri)` with `cinna.content_kind="file"` and `cinna.file_*` metadata; signed download URLs let native clients fetch without a session JWT; no new A2A routes — `GET /external/sessions/{id}/messages` already replays the full message including FileParts
- **[External Agent Access](../../application/external_agent_access/external_agent_access.md)** — Cinna Desktop and Mobile receive FileParts automatically through the existing `_build_parts_for_session_message` path
- **[Chat Windows](../../application/chat_interface/chat_windows.md)** — `StreamEventRenderer` routes `attachment` events to `AttachmentBlock` and `attachment_error` events to the error variant; `MessageBubble` handles the `message.files` fallback for agent attachments; `WebappChatWidget` handles `attachment_error` events in the embedded webapp chat stream handler
- **[Guest Sharing](../guest_sharing/guest_sharing.md)** — download and preview are accessible through the guest download-permission rule (guest → agent owner is the file owner)

## Future Enhancements (Out of Scope)

- **Mid-stream placeholder** — a "preparing attachment" chip visible while the stream is still running, replaced by the real card at finalize
- **`FileWithBytes` inline A2A transport** — embed small files directly in the `FilePart` payload for fully offline clients (currently always `FileWithUri`)
- **Structured MCP `attach_file` tool** — a typed tool alternative to the text-tag convention for SDKs that prefer explicit tool calls
- **Inbound user-to-agent file parts over A2A** — letting native clients send `FilePart` items to agents (this feature covers agent-to-user direction only)
- **Attachment thumbnails** — server-side image thumbnail generation

---

*Last updated: 2026-06-06*
