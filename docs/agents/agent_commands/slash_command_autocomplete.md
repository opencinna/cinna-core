# Slash Command Autocomplete

## Overview

The slash command autocomplete feature adds a TUI/CLI-style popup to the chat session message input. When a user begins typing `/`, a popup appears above the input listing all available slash commands with their descriptions and current availability status. The popup filters in real time as the user types, supports keyboard navigation, and allows commands to be executed directly from the popup.

This feature is only active on the authenticated session page (`/session/:sessionId`). It is not present on guest share pages, webapp widget chats, or any other context where the `sessionId` prop is not passed to `MessageInput`.

---

## User Flow

1. User opens a chat session page.
2. User types `/` in the message input.
3. A popup appears above the input row listing all registered commands with names and descriptions.
4. As the user types more (e.g. `/fi`), the popup filters to matching commands only.
5. Commands that are currently unavailable (e.g. `/rebuild-env` during an active stream) appear grayed out with an "Unavailable" badge.
6. The user can:
   - Press **ArrowDown / ArrowUp** to move through available commands (unavailable commands are skipped).
   - Press **Tab** to autocomplete the selected command name into the textarea (with a trailing space).
   - Press **Enter** to execute the selected command immediately.
   - Press **Escape** to dismiss the popup without sending.
   - **Click** an available command to execute it.
7. Clicking or executing an unavailable command has no effect.
8. When the input is cleared or no longer starts with `/`, the popup is dismissed.

---

## Command Availability

| Command | Available | Condition |
|---------|-----------|-----------|
| `/files` | Always | No conditions |
| `/files-all` | Always | No conditions |
| `/session-recover` | Always | No conditions |
| `/session-reset` | Always | No conditions |
| `/webapp` | Always | No conditions |
| `/rebuild-env` | Conditional | Unavailable when any session on the same environment is actively streaming |

The availability of `/rebuild-env` mirrors the runtime check in `RebuildEnvCommandHandler.execute()`. The backend recomputes this on every call to the commands endpoint, so the popup reflects real-time state.

---

## Backend API

**Endpoint**: `GET /api/v1/sessions/{session_id}/commands`

**Authentication**: Authenticated users only (`CurrentUser` dependency). Guest access is not supported — the popup is a UX aid for the main session page which requires login.

**Access control**: Session must belong to the requesting user. Superusers can access any session's commands.

**Response model**: `SessionCommandsPublic`

```json
{
  "commands": [
    {
      "name": "/files",
      "description": "List user-facing workspace files with clickable links",
      "is_available": true
    },
    {
      "name": "/rebuild-env",
      "description": "Rebuild the active environment for this agent",
      "is_available": false
    }
  ]
}
```

**Error responses**:
- `404 Not Found` — session does not exist
- `400 Bad Request` — session belongs to another user
- `401 Unauthorized` — no authentication token provided

**Notes**:
- Commands are returned in registration order (Python dict insertion order, preserved since Python 3.7).
- The `/rebuild-env` availability check queries the `active_streaming_manager` for all sessions on the same environment. If the check fails due to an exception, `is_available` defaults to `true` to avoid blocking the listing.
- No database changes are needed — this endpoint is entirely read-only.

---

## Frontend Components

### `SlashCommandPopup`

**File**: `frontend/src/components/Chat/SlashCommandPopup.tsx`

A purely presentational component that renders the floating command list. It accepts the already-filtered command list and selected index from `MessageInput` — it has no internal state and performs no data fetching.

**Props**:
| Prop | Type | Description |
|------|------|-------------|
| `commands` | `SessionCommandPublic[]` | Filtered list of commands to display |
| `selectedIndex` | `number` | Index of the highlighted command (-1 means no selection) |
| `onSelect` | `(command) => void` | Called when the user clicks an available command |
| `filter` | `string` | Current input text (passed through but not used internally) |

**Behavior**:
- Renders nothing when `commands` is empty.
- Positions itself above the input row using `absolute bottom-full`.
- Scrolls the selected item into view when `selectedIndex` changes.
- Unavailable commands are shown with reduced opacity and an "Unavailable" badge; clicks on them are no-ops.
- Uses `role="listbox"` and `role="option"` for accessibility.

### `MessageInput` modifications

**File**: `frontend/src/components/Chat/MessageInput.tsx`

A new optional `sessionId?: string` prop enables the feature. When not provided, the component behaves exactly as before.

**New state**:
- `showCommandPopup: boolean` — controls popup visibility
- `selectedCommandIndex: number` — which command is highlighted (-1 = none)

**New query**:
```typescript
useQuery({
  queryKey: ["sessionCommands", sessionId],
  queryFn: () => MessagesService.listSessionCommands({ sessionId }),
  enabled: !!sessionId && showCommandPopup,
  staleTime: 30_000,
})
```

The query is only enabled when the popup is showing, reducing unnecessary API calls.

**Derived state**:
```typescript
filteredCommands = commandsData.commands.filter(cmd => cmd.name.startsWith(message.toLowerCase()))
```

**Keyboard bindings** (only when popup is open and filteredCommands is non-empty):
| Key | Action |
|-----|--------|
| `ArrowDown` | Move selection down, skip unavailable commands, wrap to first available |
| `ArrowUp` | Move selection up, skip unavailable commands, wrap to last available |
| `Tab` | Autocomplete selected (or first available) command into textarea |
| `Enter` | Execute selected available command; falls through to normal send if none selected |
| `Escape` | Dismiss popup |

**onChange logic**: When input starts with `/` and `sessionId` is present, `showCommandPopup` is set to `true`. Otherwise it is `false`.

**`handleSend` is unchanged.** When Enter is pressed with a selected popup command, the handler calls `onSend(cmd.name, fileIds)` directly rather than via `handleSend()`, because React state updates are asynchronous.

---

## Integration Points

- **No changes** to `useSessionStreaming.ts`, `session_service.py`, or any command handler — command execution flows through the existing `onSend` path unchanged.
- **No changes** to guest share, webapp widget, or A2A paths — those contexts do not pass `sessionId` to `MessageInput`, so the feature is silently inactive.
- The auto-generated client (`frontend/src/client/`) was regenerated after adding the backend endpoint. `MessagesService.listSessionCommands()`, `SessionCommandPublic`, and `SessionCommandsPublic` are all auto-generated — do not edit them manually.

---

## No Database Changes

This feature adds only new Pydantic response models (`SessionCommandPublic`, `SessionCommandsPublic`) and a new read-only API endpoint. No Alembic migrations are required.

---

## Future Enhancements

See `docs/drafts/slash-command-autocomplete_plan.md` for out-of-scope ideas including per-command argument hints, command execution history, custom availability rules per handler, and guest share support.
