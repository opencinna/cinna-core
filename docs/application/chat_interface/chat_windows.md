# Chat Windows

## Purpose

Chat windows are the primary interaction surface between users and AI agents across the platform. They render streaming message conversations with rich content — markdown, tool call visualizations, file attachments, interactive widgets — in multiple hosting contexts: full session pages, guest share pages, and embedded webapp widgets.

## Core Concepts

- **Chat Window** — A scrollable message thread with input area, supporting real-time streaming display. Reuses the same core rendering components across all hosting contexts
- **Message Bubble** — Visual container for a single message. Differentiates user messages (right-aligned, green tint), agent messages (left-aligned, muted background), and system messages (centered, contextual styling)
- **Streaming Message** — A live-updating message rendered from WebSocket events during agent processing. Replaced by a persisted MessageBubble once the stream completes
- **Stream Event** — A single unit of streaming output: assistant text, tool call, thinking block, system notification, or webapp action. Each event has a sequential `event_seq` for ordering and deduplication
- **Conversation Mode UI** — Display density toggle: `detailed` (full tool output, thinking blocks visible) vs `compact` (simplified tool display, thinking blocks hidden)
- **Tool Call Block** — Specialized visual renderer for agent tool usage. Each tool type (Read, Write, Edit, Bash, etc.) has a dedicated block component with contextual display
- **Stick-to-Bottom** — Auto-scroll behavior that keeps the chat viewport pinned to the latest content during streaming, unless the user manually scrolls up

## Hosting Contexts

Chat windows appear in three distinct contexts, each with different capabilities:

### 1. Session Page (Full-Featured)

The primary chat experience for authenticated users managing agent sessions.

- Full message history with streaming
- File upload and attachment support (drag-drop, 100MB limit)
- Mode switching between building and conversation
- Sub-tasks panel showing delegated tasks with status badges
- Environment panel for workspace file browsing
- Session recovery on system errors
- Answer questions and tool approval modals
- Chat header with title, mode indicator, integration badges

### 2. Guest Share Page

Read/write chat access for unauthenticated users via share token.

- Security code verification flow before access
- Session creation and selection from existing sessions
- Conversation mode only (building mode blocked)
- Environment panel access (if configured in share settings)
- Same MessageList/MessageBubble rendering as session page
- Guest JWT authentication scoped to the share

### 3. Webapp Chat Widget (Embedded)

Collapsible floating chat widget embedded alongside agent webapp dashboards. Has unique behaviors including localStorage caching, page context collection, webapp action forwarding, and self-contained streaming. See **[Webapp Chat Widget](webapp_chat_widget.md)** for the full aspect documentation.

## Message Types & Rendering

### User Messages
- Right-aligned green-tinted bubble
- Plain text with whitespace preservation
- Email integration badge when `integration_type="email"`
- File attachment badges with download links

### Agent Messages
- Left-aligned muted bubble
- Content rendered via `StreamEventRenderer` — iterates streaming events in sequence
- Command responses (e.g., `/files`) rendered as plain markdown instead of streaming events
- Metadata footer: timestamp, model name, cost, duration, turn count (hover tooltip)
- Status badges: yellow "Interrupted" or red "Error" when applicable

### System Messages
- Centered, contextual styling:
  - **Error** — red destructive background with "Recover Session" button
  - **Task created** — blue background with navigation link (to task or session)
  - **General** — muted background
- System errors in active sessions trigger RecoverSessionModal availability

### Task Feedback Messages
- Inline centered messages from sub-task agent results
- Color-coded by state: green (completed), amber (needs_input), red (error)
- Link to sub-task session for navigation

## Special Widgets Inside Chat

### Markdown Rendering

Shared component providing GitHub-flavored markdown display with custom code block styling. See **[Markdown Rendering](markdown_rendering.md)** for details.

### Tool Call Blocks

14+ specialized tool renderers dispatched by tool name, with compact mode variants. See **[Tool Rendering](tool_rendering.md)** for the full registry and dispatch logic.

### Compact Mode
- Toggle available in UI (header or input area)
- Hides thinking blocks entirely
- Simplifies Read/Edit/Bash tool display to just filenames or shortened commands
- Controlled by `conversationModeUi` prop: `"detailed"` or `"compact"`
- Webapp widget auto-selects: conversation mode → compact, building mode → detailed

### Answer Questions Widget

Modal for answering structured agent questions mid-conversation. Supports single-select, multi-select, and custom text input with deduplication across tool calls. See **[AskUserQuestion Widget](tool_answer_questions_widget.md)** for the full flow, component structure, and answer formatting.

### Tool Approval Widget

One-click approval of agent tool usage directly from chat. Adds tools to agent's allowed list and auto-resumes conversation. See **[Tool Approval Widget](tool_approval_widget.md)** for the full flow and business rules.

### Session Recovery Widget
- Modal triggered by "Recover Session" button on system error messages
- Detects if the failed user message can be auto-resent or requires manual continuation
- Shows appropriate recovery action and toast feedback

### File Attachments
- File upload via MessageInput with drag-drop support (FileUploadModal)
- 100MB file size limit
- Attached files displayed as FileBadge components with download capability
- Files shown in message footer below content

## Auto-Scroll & Streaming Display

The chat maintains a "stick to bottom" pattern during streaming with user scroll detection, zone-based message splitting, and dynamic polling intervals. See **[Auto-Scroll & Streaming Display](auto_scroll_and_streaming_display.md)** for the full behavior specification and **[tech](auto_scroll_and_streaming_display_tech.md)** for implementation details.

Key behaviors:
- Auto-scrolls to latest content unless user manually scrolls up (>100px threshold)
- Floating scroll-down button when user scrolls away from bottom
- During streaming, messages split into before/during/after zones to prevent ordering artifacts
- Webapp widget uses simpler always-scroll behavior
- When a chat widget opens with existing message history (e.g. restored from cache), it must scroll to bottom immediately — same stick-to-bottom behavior as the session page
- Message list must remain visible during background refreshes (e.g. after send or stream completion) — never replace with a loading spinner mid-conversation
- Message input must receive focus when the chat widget opens
- Message input must retain focus across send and streaming lifecycle — re-focus after textarea is re-enabled post-send, and after streaming ends (iframe webapp actions can steal focus during streaming)

## Business Rules

- Chat windows require an active session linked to an agent environment
- Messages are immutable once created — content updates only occur during streaming via incremental DB flushes
- Streaming state is derived from `session.interaction_status`, not local component state
- Compact mode is a UI-only preference — does not affect what data is stored or streamed
- Guest share chats are forced to conversation mode
- Webapp widget chats inherit mode from webapp configuration
- File uploads are only available in full session page context (not guest share or webapp widget input)
- The webapp widget caches session and messages in localStorage for instant display across page navigations

## Integration Points

- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — Session lifecycle, status tracking, result states that drive chat behavior
- **[Streaming Architecture](../realtime_events/frontend_backend_agentenv_streaming.md)** — WebSocket event bus, event sequencing, deduplication, incremental persistence
- **[Agent Commands](../../agents/agent_commands/agent_commands.md)** — `/files` and other slash commands processed as special message types
- **[Agent Handover](../../agents/agent_handover/agent_handover.md)** — Sub-tasks panel and task feedback messages in chat
- **[Input Tasks](../input_tasks/input_tasks.md)** — Task-linked sessions with bidirectional status sync
- **[Guest Sharing](../../agents/agent_sharing/guest_sharing.md)** — Guest share page authentication and session scoping
- **[Webapp Chat](../../agents/agent_webapp/webapp_chat.md)** — Webapp-specific chat endpoints, page context, and action forwarding
- **[Tools Approval](../../agents/agent_environment_core/tools_approval_management.md)** — Tool approval flow triggered from chat message actions
- **[Ask User Question Widget](tool_answer_questions_widget.md)** — Detailed question answering flow documentation
- **[Tool Approval Widget](tool_approval_widget.md)** — Tool approval flow from chat messages
