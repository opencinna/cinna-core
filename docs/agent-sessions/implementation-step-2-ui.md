# Agent Sessions - Step 2: UI Implementation Plan

**Date:** 2025-12-23
**Status:** 📝 Planning
**Dependencies:** Step 1 (Data Layer) ✅ Completed

---

## Overview

Implement React UI for agent sessions with chat interface. Initial implementation uses URL-based routing (full page load for messages). Architecture designed for future WebSocket layer to receive real-time updates without page reload.

---

## Route Structure

```
frontend/src/routes/_layout/
├── agents.tsx                          # ✅ Existing - Agent grid
├── agent/
│   └── $agentId.tsx                    # ✅ Existing - Agent detail with tabs
├── sessions.tsx                        # NEW - All sessions list
└── session/
    └── $sessionId.tsx                  # NEW - Chat interface
```

**Route URLs:**
- `/agents` - Agent management (existing)
- `/agent/{agentId}` - Agent detail with new Environments tab (extend existing)
- `/sessions` - List all user sessions (filterable by agent/mode)
- `/session/{sessionId}` - Chat interface with message history

---

## Component Structure

```
frontend/src/components/
├── Agents/                             # ✅ Existing
│   ├── AgentCard.tsx
│   ├── AgentPromptsTab.tsx
│   ├── AgentCredentialsTab.tsx
│   └── AgentEnvironmentsTab.tsx       # NEW - Environment management
├── Environments/                       # NEW
│   ├── EnvironmentCard.tsx            # Environment status card
│   ├── AddEnvironment.tsx             # Create environment dialog
│   ├── EnvironmentStatusBadge.tsx     # Status indicator
│   └── EnvironmentActions.tsx         # Start/Stop/Activate buttons
├── Sessions/                           # NEW
│   ├── SessionCard.tsx                # Session card with mode badge
│   ├── CreateSession.tsx              # Session creation dialog (from dashboard)
│   └── SessionModeBadge.tsx           # 🔨 / 💬 indicator
└── Chat/                               # NEW
    ├── ChatHeader.tsx                 # Session title + mode toggle + back button
    ├── MessageList.tsx                # Scrollable message container
    ├── MessageBubble.tsx              # Individual message (user/agent/system)
    ├── MessageInput.tsx               # Text input + send button
    ├── ModeSwitchToggle.tsx           # Building ↔ Conversation mode switch
    └── StreamingIndicator.tsx         # "Agent is typing..." (future websocket)
```

---

## 1. Agent Detail Page - Environments Tab

**File:** `frontend/src/components/Agents/AgentEnvironmentsTab.tsx`

**Purpose:** Show agent's runtime environments, manage lifecycle

**Layout:**
- List layout (vertical stack, not grid)
- Active environment displayed at the top
- Remaining environments sorted by updated_at (newest first)
- Each list item shows: name, version, status badge, last health check, actions
- Actions: Start/Stop, Set Active, View Logs, Delete
- "Add Environment" button at top

**Environment Status Colors:**
- `stopped` - Gray
- `starting` - Yellow/orange (animated pulse)
- `running` - Green
- `error` - Red
- `deprecated` - Muted gray with strikethrough

**Active Environment Indicator:**
- Green checkmark badge + "Active" label
- Highlighted background (subtle green tint)
- Always shown first in list
- Only one can be active at a time

**Key Fields:**
```typescript
interface EnvironmentCard {
  id: string
  instance_name: string        // "Production", "Testing"
  env_name: string              // "python-env-basic"
  env_version: string           // "1.0.0"
  status: "stopped" | "starting" | "running" | "error" | "deprecated"
  is_active: boolean
  last_health_check: string | null
}
```

**API Calls:**
- `GET /agents/{agentId}/environments` - Load environments
- `POST /environments/{envId}/start` - Start environment
- `POST /environments/{envId}/stop` - Stop environment
- `POST /agents/{agentId}/environments/{envId}/activate` - Set active

---

## 2. Sessions List Page

**File:** `frontend/src/routes/_layout/sessions.tsx`

**Purpose:** Show all user sessions across all agents

**Layout:**
- Card-based grid (like agents/credentials)
- Each card shows:
  - Session title (auto-generated or user-set)
  - Agent name (small badge)
  - Mode badge (🔨 Building / 💬 Conversation)
  - Last message timestamp
  - Status badge (active/paused/completed/error)
  - Last message preview (truncated)

**Filters:**
- By agent (dropdown)
- By mode (Building / Conversation / All)
- By status (Active / Paused / Completed)

**Card Click Action:** Navigate to `/session/{sessionId}`

**Empty State:**
- "No sessions yet"
- "Create a new session from the dashboard"

---

## 3. Dashboard - Session Creation

**File:** `frontend/src/routes/_layout/index.tsx` (extend existing)

**Add Section:** "Quick Start" or "New Session"

**Session Creation Dialog:**
- Agent selector (dropdown, only agents with active environment)
- Mode selector:
  - Radio buttons: 🔨 Building Mode / 💬 Conversation Mode
  - Default: Conversation
  - Helper text explaining each mode
- Title (optional, auto-generate if empty)
- Create button → Navigate to `/session/{sessionId}`

**Creation Flow:**
1. User selects agent
2. Backend checks `agent.active_environment_id`
3. If no active environment → Show error: "Please start an environment first"
4. If environment not running → Show error: "Environment is {status}, please start it"
5. If valid → Create session with `environment_id` and `mode`
6. Navigate to chat interface

---

## 4. Chat Interface - Full Page Load Architecture

**File:** `frontend/src/routes/_layout/session/$sessionId.tsx`

### 4.1 Component Tree

```tsx
<ChatPage>
  <ChatHeader
    session={session}
    onModeSwitch={handleModeSwitch}
    onBack={() => navigate('/sessions')}
  />
  <MessageList messages={messages} isLoading={loadingMessages} />
  <MessageInput
    onSend={handleSendMessage}
    disabled={sendingMessage || session.status !== 'active'}
  />
</ChatPage>
```

### 4.2 Data Loading Pattern

**useQuery for session metadata:**
```typescript
const { data: session } = useQuery({
  queryKey: ["session", sessionId],
  queryFn: () => SessionsService.readSession({ id: sessionId }),
  enabled: !!sessionId,
  refetchInterval: 10000, // Poll for status updates every 10s
})
```

**useQuery for messages:**
```typescript
const { data: messagesData, isLoading: loadingMessages } = useQuery({
  queryKey: ["messages", sessionId],
  queryFn: () => MessagesService.getSessionMessages({
    sessionId,
    skip: 0,
    limit: 100
  }),
  enabled: !!sessionId,
})
```

**useMutation for sending messages:**
```typescript
const sendMessageMutation = useMutation({
  mutationFn: (content: string) =>
    MessagesService.sendMessage({
      sessionId,
      requestBody: { content }
    }),
  onSuccess: () => {
    // Refetch messages to get the agent's response
    queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
  },
})
```

### 4.3 Message Rendering

**MessageBubble Component:**
- `role: "user" | "agent" | "system"`
- User messages: right-aligned, primary color background
- Agent messages: left-aligned, secondary color background
- System messages: center-aligned, muted background
- Timestamp display (relative: "2 minutes ago")
- Markdown rendering for agent messages (use `react-markdown`)

**Message Structure:**
```typescript
interface Message {
  id: string
  session_id: string
  role: "user" | "agent" | "system"
  content: string
  timestamp: string
  message_metadata: {
    mock?: boolean           // From Step 1 stub
    token_count?: number     // Future
    model?: string           // Future
  } | null
  sequence_number: number
}
```

### 4.4 Auto-Scroll Behavior

- Scroll to bottom on mount
- Scroll to bottom when new message arrives
- Stay at scroll position when user scrolls up (reading history)
- "Scroll to bottom" button appears when scrolled up + new message

### 4.5 Loading States

- **Initial load:** Full-page spinner
- **Sending message:** Disable input, show "Sending..." in button
- **Agent responding:** Show typing indicator (animated dots)
- **Error:** Toast notification + enable retry

---

## 5. Session Modes - UI/UX

### 5.1 Mode Badge Component

**File:** `frontend/src/components/Sessions/SessionModeBadge.tsx`

**Building Mode:**
- Icon: 🔨 (Hammer)
- Color: Orange/amber
- Label: "Building Mode"

**Conversation Mode:**
- Icon: 💬 (Speech bubble)
- Color: Blue/primary
- Label: "Conversation Mode"

### 5.2 Mode Toggle in Chat Header

**Component:** `ModeSwitchToggle.tsx`

**UI Pattern:** Toggle switch (shadcn/ui Switch component)

**Layout:**
```
┌──────────────────────────────────────────┐
│ 💬 Conversation Mode [Toggle] 🔨         │
│ Quick task execution                     │
└──────────────────────────────────────────┘
```

**On Toggle:**
1. Call `PATCH /sessions/{id}/mode` with new mode
2. Update local state
3. Show toast: "Switched to Building Mode"
4. No confirmation dialog needed - allow instant switching

### 5.3 Mode-Specific UI Elements

**Building Mode (Orange Theme):**
- Header background: Orange gradient
- Mode badge: Orange
- Helper text: "You can create scripts, modify files, and configure integrations"
- Tips panel (collapsible):
  - "Ask the agent to create Python scripts"
  - "Configure external service integrations"
  - "Set up automation workflows"

**Conversation Mode (Blue Theme):**
- Header background: Blue gradient
- Mode badge: Blue/primary
- Helper text: "Use built-in tools for quick task execution"
- Tips panel (collapsible):
  - "Execute pre-built workflows"
  - "Query processed data"
  - "Get task summaries"

---

## 6. Future: WebSocket Integration (Architecture Notes)

**Not implemented in Step 2, but UI designed to support it.**

### 6.1 WebSocket Message Types

```typescript
type WSMessageType =
  | "message_start"       // Agent starts generating
  | "message_chunk"       // Streaming text chunk
  | "message_end"         // Agent finished
  | "tool_execution_start" // Tool/function call started
  | "tool_execution_update" // Tool progress update
  | "tool_execution_end"   // Tool finished
  | "error"               // Error occurred

interface WSMessage {
  type: WSMessageType
  session_id: string
  message_id?: string     // For message events
  content?: string        // For chunks
  metadata?: any
  timestamp: string
}
```

### 6.2 Chat Component WebSocket Modifications

**Add WebSocket hook:**
```typescript
const { connected, messages: wsMessages } = useSessionWebSocket(sessionId)
```

**Rendering Logic:**
- If websocket connected: Render streaming chunks in real-time
- If websocket disconnected: Fall back to polling (`refetchInterval`)
- Tool execution events: Show progress indicators below message
- Error events: Show inline error message

**Message Display:**
- Completed messages: Render from database query
- Streaming message: Render from WebSocket chunks (append to DOM)
- When `message_end` received: Refetch messages from DB to get final version

### 6.3 Streaming Indicator Component

**File:** `frontend/src/components/Chat/StreamingIndicator.tsx`

**UI:**
- Animated typing dots
- Show during `message_start` → `message_end`
- Hide when message complete

**Tool Execution Indicator:**
- Show when `tool_execution_start` received
- Display tool name and progress
- Update on `tool_execution_update`
- Hide on `tool_execution_end`

---

## 7. API Endpoints Used

### Backend Endpoints (from Step 1):

**Sessions:**
- `POST /sessions` - Create session (requires `agent_id`, optional `mode`)
- `GET /sessions` - List user sessions
- `GET /sessions/{id}` - Get session details
- `PATCH /sessions/{id}/mode` - Switch mode
- `DELETE /sessions/{id}` - Delete session

**Messages:**
- `GET /sessions/{sessionId}/messages` - Get message history
- `POST /sessions/{sessionId}/messages` - Send message (returns agent response)

**Environments:**
- `GET /agents/{agentId}/environments` - List environments
- `POST /agents/{agentId}/environments` - Create environment
- `POST /environments/{id}/start` - Start environment
- `POST /environments/{id}/stop` - Stop environment
- `POST /agents/{agentId}/environments/{envId}/activate` - Set active

---

## 8. Key Implementation Patterns

### 8.1 Error Handling

**Environment not running:**
```typescript
if (session?.environment?.status !== 'running') {
  return (
    <Alert variant="warning">
      Environment is {session.environment.status}.
      <Button onClick={startEnvironment}>Start Environment</Button>
    </Alert>
  )
}
```

**Session in error state:**
```typescript
if (session?.status === 'error') {
  return (
    <Alert variant="destructive">
      Session encountered an error.
      <Button onClick={retrySession}>Retry</Button>
    </Alert>
  )
}
```

### 8.2 Optimistic Updates

**Sending message:**
1. Add user message to local state immediately (optimistic)
2. Show "Sending..." state
3. Send API request
4. On success: Replace optimistic message with real message + agent response
5. On error: Remove optimistic message, show error toast

### 8.3 Polling Strategy

**Session metadata:** Poll every 10s for environment status updates
**Messages:** Don't poll - only refetch after sending message
**Environment health:** Poll every 30s on environment tab

---

## 9. UI Polish Requirements

### 9.1 Responsive Design

- Desktop: Sidebar navigation + full chat
- Tablet: Collapsible sidebar
- Mobile: Bottom navigation + full-screen chat

### 9.2 Keyboard Shortcuts

- `Enter` - Send message
- `Shift + Enter` - New line in input
- `Esc` - Close dialogs

### 9.3 Accessibility

- ARIA labels for all interactive elements
- Keyboard navigation support
- Screen reader announcements for new messages
- Focus management in dialogs

### 9.4 Loading Skeletons

- Use shadcn/ui Skeleton component
- Message list: Show 3-5 skeleton messages while loading
- Session cards: Skeleton cards during fetch

---

## 10. State Management

**No Redux/Zustand needed** - Use React Query for all server state:

- `["agents"]` - Agent list
- `["agent", agentId]` - Agent detail
- `["environments", agentId]` - Agent environments
- `["environment", envId]` - Environment detail
- `["sessions"]` - Session list
- `["session", sessionId]` - Session detail
- `["messages", sessionId]` - Message history

**Local state only:**
- Text input value
- UI toggles (mode switch confirmation)
- Scroll position tracking

---

## 11. Testing Checklist

### 11.1 Environment Tab
- [ ] Load environments for agent
- [ ] Start/stop environment updates status
- [ ] Activate environment shows checkmark
- [ ] Status badge colors correct
- [ ] Can create new environment

### 11.2 Session Creation
- [ ] Agent selector shows only agents with active environments
- [ ] Mode selector works (Building/Conversation)
- [ ] Creates session and navigates to chat
- [ ] Error shown if environment not running

### 11.3 Chat Interface
- [ ] Messages load on mount
- [ ] Can send message
- [ ] Agent response appears after sending
- [ ] User/agent message styling correct
- [ ] Timestamps display correctly
- [ ] Auto-scroll works
- [ ] Markdown renders in agent messages

### 11.4 Mode Switching
- [ ] Mode badge displays correctly
- [ ] Toggle switches mode instantly (no confirmation)
- [ ] Mode switches successfully
- [ ] Theme changes (orange/blue)
- [ ] Helper text updates

### 11.5 Session List
- [ ] Sessions load and display
- [ ] Filter by mode works
- [ ] Filter by agent works
- [ ] Click navigates to chat
- [ ] Mode badges show correctly

---

## 12. Implementation Order

**Recommended sequence:**

1. **Environments Tab** (extends existing agent detail)
   - `AgentEnvironmentsTab.tsx`
   - `EnvironmentCard.tsx`
   - `AddEnvironment.tsx`
   - Wire up to existing agent detail page tabs

2. **Session Creation** (simple dialog)
   - `CreateSession.tsx` dialog component
   - Add to dashboard
   - Basic validation

3. **Sessions List** (card grid, similar to agents/credentials)
   - `/sessions` route
   - `SessionCard.tsx`
   - Filters

4. **Chat Interface - Basic** (full page load)
   - `/session/{sessionId}` route
   - `ChatHeader.tsx`
   - `MessageList.tsx`
   - `MessageBubble.tsx`
   - `MessageInput.tsx`
   - Load messages, send message, refetch

5. **Session Modes** (add mode toggle and styling)
   - `ModeSwitchToggle.tsx`
   - `SessionModeBadge.tsx`
   - Mode-specific theming
   - Mode switch API integration

6. **Polish** (after core functionality works)
   - Loading states
   - Error handling
   - Optimistic updates
   - Auto-scroll
   - Keyboard shortcuts
   - Mobile responsive

7. **Future: WebSocket** (separate PR/ticket)
   - WebSocket connection hook
   - Streaming message rendering
   - Tool execution indicators
   - Real-time updates without refetch

---

## 13. Critical Patterns from Development Guidelines

**From `docs/development_guidelines_llm.md`:**

### Route Structure (CRITICAL)
✅ Use: `session/$sessionId.tsx`
❌ Avoid: `sessions.$sessionId.tsx` (causes routing issues)

### Query Pattern
✅ Use `useQuery` for detail pages (not `useSuspenseQuery`)
✅ Use `enabled: !!sessionId` to prevent query when param undefined
✅ Explicit loading/error states

### Card-Based UI
✅ Entire card wrapped in `<Link>` for clickability
✅ `break-words` on titles (not `truncate`)
✅ Conditional rendering for optional fields (no placeholder text)
✅ Hover effects: `hover:shadow-md hover:-translate-y-0.5`

### Form Handling
✅ Use `handleError.bind(showErrorToast)` for mutation errors
✅ Invalidate queries in `onSettled`, not `onSuccess`

### Navigation
✅ After creation: Navigate to detail page (not back to list)
✅ Use `navigate({ to: "/session/$sessionId", params: { sessionId } })`

---

## 14. Success Criteria

### Must Have
✅ Can create environment for agent from UI
✅ Can start/stop environment from UI
✅ Can create new session with mode selection
✅ Can view session list with mode badges
✅ Can open chat interface and see message history
✅ Can send messages and see responses
✅ Can switch session mode via toggle
✅ Mode-specific theming works
✅ All error states handled gracefully
✅ Mobile responsive

### Nice to Have
- Auto-generate session titles from first message
- Session search
- Export session as JSON
- Copy message content button
- Message timestamps relative ("2 min ago")

### Future (Post Step 2)
- WebSocket streaming
- Tool execution indicators
- File upload in chat
- Voice input
- Multi-agent collaboration UI

---

**Document Status:** Draft
**Last Updated:** 2025-12-23
**Next Steps:** Review and approve, then implement in order 1-6