# Slash Command Autocomplete UI — Implementation Plan

## Overview

This feature adds a TUI/CLI-style slash command autocomplete popup to the chat session's message input. When a user begins typing `/`, a popup appears above the input area listing all available commands with descriptions. The list filters as the user types, supports keyboard navigation (Up/Down), autocomplete via Tab, and command execution via Enter. The command list is fetched from the backend on every activation, allowing the backend to reflect real-time availability (e.g., `/rebuild-env` disabled while streaming).

**Core capabilities:**
- Backend endpoint `GET /api/v1/sessions/{session_id}/commands` serving a list of commands with `name`, `description`, and `is_available`
- New `SlashCommandPopup` frontend component rendered above the input row inside `MessageInput`
- Keyboard navigation: Up/Down arrows, Enter to execute, Tab to autocomplete, Escape to dismiss
- Real-time filtering as user types after the `/` prefix
- Visual distinction for unavailable commands (grayed out, non-selectable)
- No database changes; no Alembic migrations

**High-level flow:**

```
User types "/"
    → MessageInput detects "/" as first character
    → Fetches GET /api/v1/sessions/{sessionId}/commands
    → SlashCommandPopup renders above input row
    → User types "/fi" → popup filters to /files, /files-all
    → Up/Down arrows → selectedIndex changes
    → Tab → autocomplete selected command into textarea
    → Enter → execute selected command (same as handleSend)
    → Escape → dismiss popup, clear selection
```

---

## Architecture Overview

```
MessageInput.tsx
├── useQuery: ["sessionCommands", sessionId]
│       → GET /api/v1/sessions/{session_id}/commands
│       → MessagesService.listSessionCommands() [auto-generated]
│
├── State: showPopup, filteredCommands, selectedIndex
│
├── SlashCommandPopup.tsx  (rendered above input row when showPopup=true)
│       → receives: commands, selectedIndex, onSelect, onAutocomplete
│       → keyboard navigation handled in MessageInput keydown handler
│
└── Textarea keydown handler
        → ArrowUp/Down: move selectedIndex
        → Tab: call onAutocomplete (write command name to textarea)
        → Enter: if popup open + command selected → execute; else normal send
        → Escape: dismiss popup
```

**Integration points with existing systems:**
- `CommandService._handlers` (backend) — source of truth for command names and descriptions
- `Session` model — read `interaction_status` and `environment_id` for availability checks
- `active_streaming_manager` — checks if any session on the environment is streaming (for `/rebuild-env`)
- `messages.py` route file — the new endpoint is added here (same prefix `/sessions`, same tag `messages`)
- `MessageInput.tsx` — primary modification point
- Auto-generated client (`frontend/src/client/`) — regenerated after backend changes

---

## Data Models

### No New Database Tables

This feature is read-only on the backend. No new tables or Alembic migrations are needed.

### New Pydantic Models (Backend — no `table=True`)

**Location**: `backend/app/models/session.py` (add alongside existing session models) or inline in `messages.py` if the models are small and single-use. Prefer `session.py` for discoverability.

**`SessionCommandPublic`** — represents a single command in the list response:
```
name: str               # e.g. "/files"
description: str        # e.g. "List user-facing workspace files with clickable links"
is_available: bool      # False if the command cannot currently be executed
```

**`SessionCommandsPublic`** — list wrapper:
```
commands: list[SessionCommandPublic]
```

These follow the existing pattern of `Public` suffix for API response models (see `SessionPublic`, `MessagesPublic`).

---

## Security Architecture

- The endpoint uses `CurrentUser` dependency (authenticated users only; no guest access needed since guests chat in conversation mode and the popup is a UX aid, not critical functionality — if `CurrentUserOrGuest` is simpler to use for consistency, that is acceptable but not required)
- The endpoint validates that the session belongs to the `current_user` (same `_verify_session_access` pattern used in `messages.py`)
- No sensitive data is returned; command names and descriptions are static strings
- No rate limiting required (the query is triggered at most once per session page load and re-triggered each time the user types `/` after clearing the input)
- The `is_available` flag does not expose internal session IDs or streaming state — only a boolean

---

## Backend Implementation

### API Route

**File**: `backend/app/api/routes/messages.py`

Add a new `GET` endpoint to the existing router (prefix `/sessions`, tag `messages`). Placing it here is natural because it sits alongside the existing session-scoped message endpoints and avoids adding a new router.

**Endpoint**: `GET /api/v1/sessions/{session_id}/commands`

**Dependencies**: `session: SessionDep`, `current_user: CurrentUser`, `session_id: uuid.UUID`

**Logic**:
1. Load the `Session` from DB; raise 404 if not found
2. Call `_verify_session_access(current_user, chat_session, session)` — raises 400/403 if unauthorized (reuse existing helper in `messages.py`)
3. Ensure commands are registered by importing `app.services.commands` (the `__init__.py` import is already done at startup via `session_service.py`; the endpoint can import `CommandService` directly)
4. For each handler in `CommandService._handlers` (in insertion order — Python dicts preserve order since 3.7):
   - Compute `is_available`:
     - For `/rebuild-env` specifically: check if any session on `chat_session.environment_id` is actively streaming using `active_streaming_manager.is_any_session_streaming(session_ids)` — same check as in `RebuildEnvCommandHandler.execute()`; if streaming → `is_available=False`
     - For all other commands: `is_available=True`
   - Build `SessionCommandPublic(name=handler.name, description=handler.description, is_available=is_available)`
5. Return `SessionCommandsPublic(commands=[...])`

**Response model**: `SessionCommandsPublic`

**Note**: The handler iteration should use `CommandService._handlers.values()` — this exposes the private dict but is consistent with how `CommandService` already works internally; no new public accessor is required for MVP. If desired, a `list_handlers()` classmethod can be added to `CommandService` as a cleaner interface (preferred, see Service Layer section).

**Signature sketch**:
```python
@router.get("/{session_id}/commands", response_model=SessionCommandsPublic)
async def list_session_commands(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
```

### Service Layer

**`CommandService` — new classmethod** (in `backend/app/services/command_service.py`):

Add `list_handlers() -> list[CommandHandler]` classmethod:
- Returns `list(cls._handlers.values())` — ordered list of all registered handlers
- Keeps the private `_handlers` dict encapsulated
- Used by the route rather than accessing `_handlers` directly

**`SessionCommandPublic` and `SessionCommandsPublic`** added to `backend/app/models/session.py` (or a new `backend/app/models/command.py` if preferred for separation — `session.py` is simpler since no new file is needed).

**Availability check for `/rebuild-env`**:

The route handler needs to check streaming status. Import `active_streaming_manager` from `app.services.active_streaming_manager` and `select` from `sqlmodel`. The check:
```
session_ids = set(db.exec(select(Session.id).where(Session.environment_id == chat_session.environment_id)).all())
is_streaming = await active_streaming_manager.is_any_session_streaming(session_ids) if session_ids else False
```

This mirrors the exact check in `RebuildEnvCommandHandler.execute()`. The route handler is `async` (uses `await`), consistent with existing async routes in `messages.py`.

**No new service file** — the logic is thin enough to live in the route handler directly, consistent with how `messages.py` handles similar inline logic.

---

## Frontend Implementation

### New Component: `SlashCommandPopup`

**File**: `frontend/src/components/Chat/SlashCommandPopup.tsx`

**Purpose**: Renders the floating command list above the input row. Receives props from `MessageInput` — no internal state or data fetching.

**Props**:
```typescript
interface SlashCommandPopupProps {
  commands: SessionCommandPublic[]       // from generated client types
  selectedIndex: number                  // which command is highlighted (-1 = none)
  onSelect: (command: SessionCommandPublic) => void   // user clicks a command
  filter: string                         // current "/xyz" typed text for highlighting
}
```

**Layout**: An absolutely-positioned `div` rendered just above the input row. The parent `MessageInput` wrapper div has `relative` positioning; the popup uses `absolute bottom-full left-0 right-0 mb-1` to anchor it above the input footer row. Z-index should be set (e.g., `z-50`) to float above other content.

**Visual design** (shadcn/ui-consistent):
- Container: `bg-background border border-border rounded-lg shadow-lg overflow-hidden`
- Header label: small muted text `text-xs text-muted-foreground px-3 py-1.5 border-b` reading "Slash Commands"
- Each command row: `flex items-center gap-3 px-3 py-2 cursor-pointer transition-colors`
  - Selected row: `bg-accent text-accent-foreground`
  - Unavailable row: `opacity-50 cursor-not-allowed` (pointer-events none or onClick guard)
  - Available, not selected: `hover:bg-accent/50`
- Command name: `font-mono text-sm font-medium` (e.g., `/files`)
- Description: `text-sm text-muted-foreground truncate flex-1`
- Unavailable badge: when `!is_available`, show a small `Badge variant="outline"` with text "Unavailable" or a lock/disabled icon from `lucide-react`

**Keyboard interaction is NOT handled inside this component** — it is handled in `MessageInput`'s `handleKeyDown`. The popup is purely presentational (renders list, highlights selected, fires `onSelect` on click).

### Modified Component: `MessageInput`

**File**: `frontend/src/components/Chat/MessageInput.tsx`

**New prop**:
```typescript
sessionId?: string   // when provided, enables slash command autocomplete
```

**New state**:
```typescript
const [showCommandPopup, setShowCommandPopup] = useState(false)
const [selectedCommandIndex, setSelectedCommandIndex] = useState(-1)
```

**New query** (inside the component, only when `sessionId` is provided):
```typescript
const { data: commandsData } = useQuery({
  queryKey: ["sessionCommands", sessionId],
  queryFn: () => MessagesService.listSessionCommands({ sessionId: sessionId! }),
  enabled: !!sessionId && showCommandPopup,   // only fetch when popup should show
  staleTime: 30_000,   // cache for 30s; commands don't change often
})
```

The service name `MessagesService` will be auto-generated from the `messages` tag on the new endpoint. The method name will be auto-generated from the operation ID.

**Derived state — filtered commands**:
```typescript
const filteredCommands = useMemo(() => {
  if (!commandsData?.commands || !message.startsWith("/")) return []
  const query = message.toLowerCase()
  return commandsData.commands.filter(cmd => cmd.name.startsWith(query))
}, [commandsData, message])
```

**Popup show/hide logic**:
- `onChange` on the textarea: if new value starts with `/` → `setShowCommandPopup(true)`; otherwise → `setShowCommandPopup(false)`, `setSelectedCommandIndex(-1)`
- When `filteredCommands` becomes empty (after typing more) → hide popup

**Updated `handleKeyDown`**:

When `showCommandPopup` is true and `filteredCommands.length > 0`:
- `ArrowDown`: `e.preventDefault()`, increment `selectedCommandIndex` (wraps at end back to 0)
- `ArrowUp`: `e.preventDefault()`, decrement `selectedCommandIndex` (wraps at 0 to last)
- `Tab`: `e.preventDefault()`, autocomplete: set `message` to `filteredCommands[selectedCommandIndex].name + " "` (or first available command if index is -1); keep popup open in case user wants to read description
- `Enter` (no Shift): if `selectedCommandIndex >= 0` and `filteredCommands[selectedCommandIndex].is_available`: `e.preventDefault()`, set `message` to `filteredCommands[selectedCommandIndex].name`, call `handleSend()`, close popup, reset index
- `Escape`: `e.preventDefault()`, close popup, reset index

When `showCommandPopup` is false: existing Enter/Shift+Enter behavior unchanged.

**`handleSend` — no changes needed**: The existing `handleSend` already trims and sends `message` as-is. When Enter is pressed with a selected command, we set `message` to the command name first, then call `handleSend()`.

**Updated outer wrapper div**: Change `className="border-t p-4 bg-background/60 shrink-0"` to `className="border-t p-4 bg-background/60 shrink-0 relative"` to establish positioning context for the popup.

**Popup placement**: Render `SlashCommandPopup` immediately before the `<div className="flex gap-2 items-end ...">` flex row (i.e., between the outer wrapper opening tag and the flex row), so it appears visually above the input:
```tsx
{showCommandPopup && filteredCommands.length > 0 && (
  <SlashCommandPopup
    commands={filteredCommands}
    selectedIndex={selectedCommandIndex}
    onSelect={handleCommandSelect}
    filter={message}
  />
)}
<div className="flex gap-2 items-end max-w-7xl mx-auto">
  ...
</div>
```

The popup itself uses `absolute bottom-full` so it floats upward from the input area's top edge.

**`handleCommandSelect`** (called when user clicks a command in the popup):
- If `command.is_available`: set `message` to `command.name`, call `handleSend()`, close popup
- If `!command.is_available`: no-op (unavailable commands are not selectable)

### Modified Route: `$sessionId.tsx`

**File**: `frontend/src/routes/_layout/session/$sessionId.tsx`

Add `sessionId` prop pass-through to `MessageInput`:
```tsx
<MessageInput
  ...existingProps
  sessionId={sessionId}   // sessionId already available from route params
/>
```

The `sessionId` is the route param already extracted in this file via `useParams` or `Route.useParams()`.

### Client Regeneration

After adding the backend endpoint and models, regenerate the frontend client:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

This will auto-generate:
- `SessionCommandPublic` and `SessionCommandsPublic` types in `frontend/src/client/types.gen.ts`
- `MessagesService.listSessionCommands()` method in `frontend/src/client/sdk.gen.ts`

Import in `MessageInput.tsx`:
```typescript
import { MessagesService } from "@/client"
import type { SessionCommandPublic } from "@/client"
```

---

## Database Migrations

**No migrations required.** This feature adds only new API response models (Pydantic, no `table=True`) and a new route. No database schema changes.

---

## Error Handling & Edge Cases

**Backend:**
- Session not found: 404 response (standard `session.get(Session, session_id)` pattern)
- Unauthorized access: 400/403 via `_verify_session_access` reuse
- `CommandService._handlers` empty (unlikely, only during tests): returns `SessionCommandsPublic(commands=[])`
- `active_streaming_manager.is_any_session_streaming` error: catch exception, log warning, default `is_available=True` (don't block listing due to streaming check failure)
- Session has no environment_id (edge case): skip streaming check, treat `/rebuild-env` as available

**Frontend:**
- `commandsData` not yet loaded when user types `/` (loading state): show popup skeleton or simply show empty popup with "Loading..." text; alternatively wait until data is available before showing popup (simplest: `enabled: !!sessionId && showCommandPopup`, popup only renders when `filteredCommands.length > 0`)
- `sessionId` prop not provided (e.g., guest share page, webapp widget that doesn't pass sessionId): feature is silently disabled; `/` typed behaves as normal text. No changes required to guest or webapp paths.
- Network error fetching commands: `useQuery` error state — popup does not show; user can still type `/command` manually and it will execute normally via existing backend detection
- All commands filtered out (user typed `/xyz` which matches nothing): `filteredCommands` is empty → `showCommandPopup` effect hides popup automatically
- Selected index becomes out-of-bounds when filter narrows: clamp `selectedCommandIndex` to `Math.min(selectedCommandIndex, filteredCommands.length - 1)` in a `useEffect` watching `filteredCommands.length`
- Tab on no selection (index = -1): autocomplete first available command if any; if none available, Tab behaves as normal (insert tab or do nothing depending on textarea behavior — safest: `e.preventDefault()` and do nothing if no command is available)
- Arrow navigation skips unavailable commands: when incrementing/decrementing `selectedCommandIndex`, skip indices where `filteredCommands[i].is_available === false` (wrap-around must also skip unavailable entries)

---

## UI/UX Considerations

**Popup positioning**: The popup must not be clipped by `overflow-hidden` ancestors. The `MessageInput` outer div uses `relative`; the popup uses `absolute bottom-full`. Test that the chat page layout doesn't clip upward-opening content; if it does, use a portal via `ReactDOM.createPortal` anchored to `document.body` (measure input position via `ref.getBoundingClientRect()`). Start with the simpler `absolute bottom-full` approach; switch to portal only if clipping occurs.

**Max height**: The popup should have a `max-h-64 overflow-y-auto` to prevent excessive height when many commands match.

**Selected index initialization**: When popup first opens (user types `/`), `selectedCommandIndex = -1` (no pre-selection). Up arrow from -1 selects last item; Down arrow from -1 selects first item.

**Accessibility**: Add `role="listbox"` to popup container, `role="option"` and `aria-selected` to each command row, `aria-expanded` to textarea when popup is open.

**Empty state**: When `filteredCommands.length === 0` (e.g., user typed `/xyz` with no match), hide the popup entirely (don't show empty popup).

**Unavailable feedback**: Hovering an unavailable command could show a tooltip: "This command is currently unavailable (a stream is in progress)". Use `Tooltip` from `@/components/ui/tooltip` — same pattern as `MessageInput`'s existing refine prompt tooltip.

**Scrolling to selected**: When selectedIndex changes via keyboard, scroll the selected row into view using `itemRef.scrollIntoView({ block: "nearest" })`.

---

## Integration Points

**No changes needed to:**
- `useSessionStreaming.ts` — command execution flows through the same `onSend` path
- `session_service.py` — command execution is unchanged; this feature only adds a listing endpoint
- Any A2A, MCP, or guest share path — `sessionId` prop not passed → feature inactive
- Any webapp widget path — same as above

**Regenerate client** after backend changes:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

**Check session page route** (`frontend/src/routes/_layout/session/$sessionId.tsx`) — confirm the route param name used (`sessionId` in route params) matches the prop name passed to `MessageInput`.

---

## Future Enhancements (Out of Scope)

- **Per-command argument hints**: Show argument syntax after the command description (e.g., `/session-recover [--auto]`). Requires adding an `args_hint: str | None` field to `CommandHandler`.
- **Command execution history**: Remember recently used commands and surface them first.
- **Custom command availability rules**: Allow handlers to declare their own `is_available(context)` check via an optional abstract method, rather than hardcoding the `/rebuild-env` check in the route.
- **Guest share support**: Pass `sessionId` to `MessageInput` in the guest share route and adapt the endpoint to support `CurrentUserOrGuest`.
- **Webapp widget support**: Add `sessionId` prop to `WebappChatWidget`'s internal message input.
- **Command palette** (Cmd+K): Full command palette triggered by keyboard shortcut, not just `/` prefix.

---

## Summary Checklist

### Backend Tasks

- [ ] Add `SessionCommandPublic` and `SessionCommandsPublic` Pydantic models to `backend/app/models/session.py` (no `table=True`)
- [ ] Export new models from `backend/app/models/__init__.py` (add to the existing `__all__` or import block)
- [ ] Add `list_handlers() -> list[CommandHandler]` classmethod to `CommandService` in `backend/app/services/command_service.py`
- [ ] Add `GET /{session_id}/commands` endpoint to `backend/app/api/routes/messages.py`:
  - Import `SessionCommandPublic`, `SessionCommandsPublic`, `CommandService`
  - Import `active_streaming_manager` and `select`
  - Reuse `_verify_session_access` helper (already in `messages.py`)
  - Iterate `CommandService.list_handlers()`, compute `is_available` per handler
  - For `/rebuild-env`: query session IDs on the environment, check `active_streaming_manager.is_any_session_streaming()`
  - Return `SessionCommandsPublic(commands=[...])`
- [ ] No Alembic migration needed

### Frontend Tasks

- [ ] Run `source ./backend/.venv/bin/activate && make gen-client` after backend changes to regenerate `frontend/src/client/`
- [ ] Create `frontend/src/components/Chat/SlashCommandPopup.tsx`:
  - Props: `commands`, `selectedIndex`, `onSelect`, `filter`
  - Visual: `bg-background border rounded-lg shadow-lg`, max-height with scroll, selected row highlighted
  - Available vs unavailable visual distinction (opacity, cursor, optional badge)
  - `role="listbox"` / `role="option"` for accessibility
- [ ] Modify `frontend/src/components/Chat/MessageInput.tsx`:
  - Add optional `sessionId?: string` prop
  - Add `showCommandPopup` and `selectedCommandIndex` state
  - Add `useQuery` for `MessagesService.listSessionCommands` (enabled only when `sessionId` present and popup is showing)
  - Add `filteredCommands` derived state (useMemo filtering by current input)
  - Add clamp effect for `selectedCommandIndex` when `filteredCommands` length changes
  - Update `onChange` handler to show/hide popup on `/` prefix detection
  - Update `handleKeyDown` for ArrowUp/Down/Tab/Enter/Escape when popup is open
  - Add `handleCommandSelect` for click selection
  - Add `relative` to outer wrapper `className`
  - Render `<SlashCommandPopup>` conditionally above the flex row
- [ ] Modify `frontend/src/routes/_layout/session/$sessionId.tsx`:
  - Pass `sessionId={sessionId}` (string form of route param) to `<MessageInput>`

### Testing & Validation Tasks

- [ ] Verify `GET /api/v1/sessions/{session_id}/commands` returns correct command list for a valid authenticated session
- [ ] Verify endpoint returns 404 for non-existent session
- [ ] Verify endpoint returns 400/403 for session belonging to another user
- [ ] Verify `/rebuild-env` `is_available=false` when a session on the same environment is actively streaming
- [ ] Verify `/rebuild-env` `is_available=true` when no session is streaming
- [ ] Verify frontend popup appears when `/` is typed as first character in input
- [ ] Verify popup does not appear when session page is loaded without `sessionId` prop (guest share, webapp widget)
- [ ] Verify filtering: typing `/fi` shows only `/files` and `/files-all`
- [ ] Verify ArrowDown/Up navigation highlights correct command and skips unavailable entries
- [ ] Verify Tab key autocompletes command text into textarea
- [ ] Verify Enter with selected command executes immediately (same as typing the command and pressing Enter)
- [ ] Verify Escape dismisses popup without sending
- [ ] Verify popup disappears when input is cleared or no longer starts with `/`
- [ ] Verify clicking an available command executes it
- [ ] Verify clicking an unavailable command does nothing
- [ ] Verify popup does not appear for webapp widget or guest share chat (no `sessionId` prop passed)
