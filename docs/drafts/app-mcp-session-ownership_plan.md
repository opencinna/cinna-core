# App MCP Session Ownership — Implementation Plan

## Overview

App MCP sessions are currently created with `user_id = caller` (the MCP client user), making them invisible to the agent owner who shared the agent. The caller also cannot find these sessions in the UI since they are tied to an agent the caller doesn't own. This creates a dead zone where nobody can see or manage these sessions.

This plan fixes session ownership so the **agent owner** sees and manages App MCP sessions, with caller tracking for audit and display purposes.

**Core changes:**
- Add `caller_id` column to Session model (parallel to existing `identity_caller_id`)
- Change App MCP session creation to set `user_id = agent.owner_id` (owner sees sessions) and `caller_id = authenticated_user_id`
- Add caller info and channel badges to session page header
- Replace text-based mode indicator with icon-based badges

**High-level flow:**

```
MCP Caller (User B)  →  App MCP Server  →  Routes to Agent (owned by Admin)
                                           ↓
                                     Session created:
                                       user_id = Admin (owner)
                                       caller_id = User B (caller)
                                       integration_type = "app_mcp"
                                           ↓
                           Admin sees session in Sessions UI
                           Session header shows: "MCP — user-b@example.com"
```

---

## Architecture Overview

### Current State

```
Session.user_id = caller_user_id    ← invisible to agent owner
Session.integration_type = "app_mcp"
No caller tracking (unlike identity_mcp which has identity_caller_id)
```

### Target State

```
Session.user_id = agent.owner_id    ← owner sees in their session list
Session.caller_id = caller_user_id  ← tracks who initiated (display only)
Session.integration_type = "app_mcp"
```

### Affected Components

| Layer | Component | Change |
|-------|-----------|--------|
| Model | `Session` | Add `caller_id` column |
| Model | `SessionPublic` / `SessionPublicExtended` | Expose `caller_id`, `caller_email`, `caller_name` |
| Service | `AppMCPRequestHandler._resolve_session()` | Set `user_id = owner`, `caller_id = caller` |
| Service | `AppMCPRequestHandler` — context_id validation | Validate `caller_id` (not `user_id`) for App MCP session resumption |
| Route | `sessions.py` — `list_sessions()` | LEFT JOIN User to resolve caller info |
| Route | `sessions.py` — `get_session()` | Include caller info in response |
| Frontend | Session page header | Add MCP channel badge + caller email badge |
| Frontend | Session page header | Replace dot+text mode indicator with icon badges |
| Migration | New Alembic migration | Add `caller_id` column + backfill |

---

## Data Models

### Session Model Changes

**File:** `backend/app/models/sessions/session.py`

**New column on `Session` table:**

| Column | Type | Description |
|--------|------|-------------|
| `caller_id` | UUID, FK → user.id (SET NULL), nullable, indexed | The user who initiated the session via App MCP. `None` for manual, email, A2A, and guest sessions. For identity_mcp sessions, the existing `identity_caller_id` continues to serve this role. |

**Why a separate column from `identity_caller_id`:** Identity MCP sessions have additional binding/assignment columns that form a cohesive set. App MCP sessions need only the caller reference. Using a separate `caller_id` keeps app_mcp simple and avoids coupling to the identity data model. The identity flow retains its existing `identity_caller_id` for backward compatibility.

**Pydantic schema changes:**

`SessionPublic` — add:
- `caller_id: uuid.UUID | None = None`

`SessionPublicExtended` — add:
- `caller_id: uuid.UUID | None = None`
- `caller_name: str | None = None` (resolved at query time from User table)
- `caller_email: str | None = None` (resolved at query time from User table)

---

## Security Architecture

### Access Control

No changes to session access rules. The session listing query remains `Session.user_id == current_user.id` — only the owner sees the session. The caller continues to interact exclusively through their MCP client.

### Context ID Validation

Currently App MCP validates `context_id` by checking `session.user_id == caller_user_id`. After this change, `user_id` will be the agent owner, so the validation must check `session.caller_id == caller_user_id` instead (for `integration_type = "app_mcp"` sessions).

---

## Backend Implementation

### API Route Changes

**File:** `backend/app/api/routes/sessions.py`

#### `list_sessions()` — add caller info to response

The query already JOINs with Agent. Add a LEFT JOIN with User (aliased as `CallerUser`) to resolve `caller_name` and `caller_email` when `caller_id` is set.

```python
from sqlalchemy.orm import aliased

CallerUser = aliased(User)

statement = (
    select(
        Session,
        Agent.id, Agent.name, Agent.ui_color_preset,
        msg_count_subq.c.message_count,
        last_msg_subq.c.last_content,
        CallerUser.full_name.label("caller_name"),
        CallerUser.email.label("caller_email"),
    )
    .join(Agent, Session.agent_id == Agent.id)
    .outerjoin(CallerUser, Session.caller_id == CallerUser.id)
    ...
)
```

Then populate the new fields in `SessionPublicExtended`:
```python
SessionPublicExtended(
    **s.model_dump(),
    ...,
    caller_name=caller_name,
    caller_email=caller_email,
)
```

#### `get_session()` endpoint — same caller info

The single-session GET endpoint must also LEFT JOIN with User to resolve caller info.

### Service Layer Changes

**File:** `backend/app/services/app_mcp/app_mcp_request_handler.py`

#### `_resolve_session()` — change session creation

**Lines ~230-257 (regular app_mcp session creation):**

Before:
```python
session = SessionService.create_session(
    db_session=db, user_id=user_id, data=session_data,
    integration_type="app_mcp",
)
```

After:
```python
session = SessionService.create_session(
    db_session=db, user_id=agent.owner_id, data=session_data,
    integration_type="app_mcp",
)
# Track the caller
session.caller_id = user_id
```

The `session_data.agent_id` already references the correct agent, so the environment resolution is unaffected.

#### `_resolve_session()` — fix context_id validation

**Lines ~180-203 (context_id resumption):**

Current validation checks `session.user_id == user_id`. After the change, for `integration_type = "app_mcp"` sessions, check `session.caller_id == user_id` instead.

```python
if existing_session.integration_type == "app_mcp":
    if existing_session.caller_id != user_id:
        # Not this caller's session, fall through to new routing
        ...
elif existing_session.integration_type == "identity_mcp":
    # Existing identity_caller_id check (unchanged)
    ...
else:
    if existing_session.user_id != user_id:
        ...
```

#### Workspace assignment

When creating the session with `user_id = agent.owner_id`, `user_workspace_id` will be `None` (default workspace) since App MCP sessions are created via service layer, not the HTTP API. The owner can see default-workspace sessions from any workspace view when workspace filter is "All".

### Message Streaming

No changes needed. The streaming pipeline uses `session.environment_id` and `session.id` — neither depends on `session.user_id`. The agent environment processes messages regardless of who owns the session record.

### WebSocket Events

Session streaming events are emitted to the `session_{id}_stream` room — no change needed; the owner views the session page and subscribes to this room normally.

User-level events (`session_interaction_status_changed`) are emitted to `user_{session.user_id}` room. After this change, `user_id` is the owner, so the owner receives real-time updates on their sessions list page. No dual-emission needed since the caller has no UI session list.

---

## Frontend Implementation

### Session Page Header Redesign

**File:** `frontend/src/routes/_layout/session/$sessionId.tsx`

**Current header subtitle (line ~398-426):**
```
[orange/blue dot] Building Mode / Conversation Mode  [Email badge] [A2A badge] [Identity badge]
```

**Proposed header subtitle:**
```
[Hammer/MessageCircle icon] Building / Conversation  [MCP badge] [Email badge] [A2A badge] [Identity badge] [Caller badge]
```

#### Replace mode dot+text with icon badge

Replace the colored dot + text with a compact icon-based indicator matching the existing `SessionModeBadge` component style:

- **Building:** `<Hammer className="h-3 w-3 text-orange-500" />` + "Building"
- **Conversation:** `<MessageCircle className="h-3 w-3 text-blue-500" />` + "Conversation"

This is a purely visual change — same info, more compact, consistent with how mode is shown elsewhere in the app (e.g., McpConnectorsCard uses MessageCircle/Wrench icons for session mode).

#### Add App MCP channel badge

For sessions with `integration_type === "app_mcp"`:

```tsx
{session.integration_type === "app_mcp" && (
  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300">
    <Plug className="h-2.5 w-2.5" />
    MCP
  </span>
)}
```

Color: emerald (green) — distinct from Email (indigo), A2A (purple), Identity (violet).

#### Add caller badge

When `session.caller_email` is present (resolved from `caller_id`):

```tsx
{session.caller_email && (
  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">
    <User className="h-2.5 w-2.5" />
    {session.caller_email}
  </span>
)}
```

This badge shows the agent owner who initiated the session via MCP.

### API Client Regeneration

After backend model changes, regenerate the frontend client:
```bash
bash scripts/generate-client.sh
```

New fields on `SessionPublicExtended`:
- `caller_id: string | null`
- `caller_name: string | null`
- `caller_email: string | null`

---

## Database Migration

### Migration: Add `caller_id` to Session table

**File:** `backend/app/alembic/versions/xxxx_add_caller_id_to_session.py`

**Upgrade:**
1. Add column `caller_id` (UUID, nullable) to `session` table
2. Add foreign key constraint: `caller_id → user.id`, `ON DELETE SET NULL`
3. Add index on `caller_id` (for efficient future queries)
4. **Backfill:** For existing sessions with `integration_type = 'app_mcp'`, move `user_id` to `caller_id` and set `user_id` to the agent owner's ID (resolved via `session.agent_id → agent.owner_id`)

**Backfill SQL:**
```sql
UPDATE session
SET caller_id = user_id,
    user_id = (SELECT owner_id FROM agent WHERE agent.id = session.agent_id)
WHERE integration_type = 'app_mcp'
  AND agent_id IS NOT NULL;
```

**Downgrade:**
1. Reverse backfill: move `caller_id` back to `user_id` for `integration_type = 'app_mcp'`
2. Drop index
3. Drop column `caller_id`

---

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| Agent deleted after session exists | `agent_id` SET NULL; session remains for owner; caller badge still shows (`caller_id` independent of agent) |
| Caller user deleted | `caller_id` SET NULL (ON DELETE SET NULL); session remains for owner, caller badge disappears |
| Existing App MCP sessions (pre-migration) | Backfill handles them — `user_id` → `caller_id`, `agent.owner_id` → `user_id` |
| Agent shared to multiple users; each creates sessions | Each session has unique `caller_id`; owner sees all sessions grouped under the agent |
| Owner sends message in MCP session from UI | Works normally — `user_id = owner`, standard session access |

---

## UI/UX Considerations

### Header Badge Layout

The subtitle area under the session title holds all badges in a horizontal flex row with `gap-1.5`. The layout order:

1. Mode indicator (icon + "Building"/"Conversation")
2. Channel badge (MCP / Email / A2A / Identity — at most one)
3. Caller badge (email — only when `caller_id` is set)

All badges use the existing inline `text-[10px] font-medium` style with colored backgrounds.

### Color Scheme

| Badge | Background | Text | Icon |
|-------|-----------|------|------|
| Building | — | orange-500 | Hammer |
| Conversation | — | blue-500 | MessageCircle |
| MCP | emerald-100/900 | emerald-700/300 | Plug |
| Email | indigo-100/900 | indigo-700/300 | Mail |
| A2A | purple-100/900 | purple-700/300 | Plug |
| Identity | violet-100/900 | violet-700/300 | UserCircle |
| Caller | gray-100/800 | gray-600/300 | User |

---

## Integration Points

- **App MCP Request Handler** (`backend/app/services/app_mcp/app_mcp_request_handler.py`) — primary change: session creation and context_id validation
- **Session Route** (`backend/app/api/routes/sessions.py`) — caller info in list/get responses
- **Session Model** (`backend/app/models/sessions/session.py`) — new column and schema fields
- **Frontend Session Page** (`frontend/src/routes/_layout/session/$sessionId.tsx`) — header redesign
- **API Client** — regenerate after backend changes

---

## Future Enhancements (Out of Scope)

- **Caller-side session visibility:** Expand listing query to `user_id = me OR caller_id = me` so callers can also see their sessions in the UI
- **Caller access control:** Allow callers to access session pages (read-only or full chat) from the platform UI
- **Dual WebSocket emission:** Emit session status events to both owner and caller rooms for real-time updates
- **Notification to owner:** Notify agent owner when a new session is initiated via App MCP (activity feed or push notification)
- **Session filtering by channel:** Add `integration_type` filter to sessions list API so users can filter for "MCP sessions only"

---

## Summary Checklist

### Backend Tasks

1. **Model:** Add `caller_id` column to `Session` (UUID, FK → user.id, SET NULL, nullable, indexed)
2. **Model:** Add `caller_id`, `caller_name`, `caller_email` to `SessionPublic` and `SessionPublicExtended` schemas
3. **Migration:** Create Alembic migration with column addition + backfill for existing `app_mcp` sessions
4. **Route — list_sessions():** LEFT JOIN User table to resolve `caller_name` and `caller_email` from `caller_id`
5. **Route — get_session():** Include caller info in single-session response
6. **Service — AppMCPRequestHandler._resolve_session():** Set `user_id = agent.owner_id`, `caller_id = user_id` on new App MCP sessions
7. **Service — AppMCPRequestHandler._resolve_session():** Fix context_id validation to check `caller_id` for `app_mcp` sessions
8. **Regenerate frontend API client** after backend changes

### Frontend Tasks

9. **Session page header:** Replace dot+text mode indicator with icon-based indicator (Hammer/MessageCircle + "Building"/"Conversation")
10. **Session page header:** Add App MCP channel badge (emerald color, Plug icon, "MCP" text) for `integration_type === "app_mcp"`
11. **Session page header:** Add caller badge showing `caller_email` when present (gray color, User icon)

### Testing & Validation

12. Verify agent owner sees App MCP sessions in their sessions list
13. Verify context_id resumption works with new caller_id validation
14. Verify backfill migration correctly reassigns existing App MCP sessions
15. Verify session page header shows correct badges for all integration types (MCP, email, A2A, identity)
16. Verify no regression for identity_mcp, email, a2a, guest, and manual sessions
