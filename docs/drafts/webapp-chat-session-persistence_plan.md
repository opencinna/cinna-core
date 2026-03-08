# Webapp Chat Session Persistence Plan

**Feature name**: `webapp-chat-session-persistence`
**Document path**: `docs/drafts/webapp-chat-session-persistence_plan.md`
**Scope**: Frontend-only change — no backend API or database modifications required.

---

## 1. Overview

Add localStorage-based persistence to the webapp chat widget so that chat sessions (session ID and message history) survive browser page refreshes. Currently, session state lives only in React component state and is lost on every refresh. The backend already persists sessions server-side scoped by `webapp_share_id`; this plan closes the frontend gap by caching the session locally and restoring it instantly on mount.

Core capabilities:
- Session ID and messages cached in localStorage on every update
- On component mount, restore cached session immediately (no loading spinner if cache exists)
- Background API call verifies the cached session is still valid and loads any new messages
- If the cached session is gone from the backend, clear the cache and start fresh
- Storage key scoped to `webappToken` to isolate different share links

High-level flow:
```
Page refresh
    |
    v
WebappChatWidget mounts
    |
    v
Read localStorage for webapp_chat_{webappToken}
    |
    |-- No cache --> normal flow (load on first open)
    |
    v
Restore sessionId + messages into state (instant, no spinner)
    |
    v
Background API verify: GET /webapp/{token}/chat/sessions
    |
    |-- Session still valid --> load fresh messages, update cache
    |-- Session gone / error --> clear cache, reset to empty state
    |
    v
User sees previous messages immediately; widget behaves as before
```

---

## 2. Architecture Overview

### System Components

Only the frontend is touched. No backend, no database, no API client regeneration.

```
localStorage
    key: webapp_chat_{webappToken}
    value: { sessionId: string, messages: MessagePublic[], cachedAt: number }
        |
        v
WebappChatWidget.tsx (modified)
    - Reads cache on mount
    - Writes cache whenever sessionId or messages change
    - Verifies cache with background API call
    - Clears cache on stale session detection
```

### Data Flow

1. **On mount**: Read `webapp_chat_{webappToken}` from localStorage.
2. **If cache exists**: Set `sessionId` and `messages` from cache immediately (synchronous, no loading state needed).
3. **Background verify**: Call `GET /webapp/{token}/chat/sessions`. If response has a session with matching ID, call `loadMessages()` to get fresh messages. If no session returned (or error), call `clearCache()` and reset state.
4. **On state change**: Whenever `sessionId` changes or `messages` changes, write updated cache to localStorage.

### Integration Points

- `frontend/src/components/Webapp/WebappChatWidget.tsx` — the only file modified
- `frontend/src/routes/webapp/$webappToken.tsx` — no changes needed; passes `webappToken` prop which becomes the cache scope key
- Backend `GET /webapp/{token}/chat/sessions` — already exists, used for background verification

---

## 3. Data Models

### localStorage Cache Entry

No new backend models. The localStorage cache structure:

```
Key: "webapp_chat_{webappToken}"   (e.g., "webapp_chat_abc123xyz")

Value (JSON-serialized):
{
  sessionId: string,           // UUID of the active session
  messages: MessagePublic[],   // Array of persisted messages (not streaming events)
  cachedAt: number             // Unix ms timestamp, for potential future TTL use
}
```

Type definition (TypeScript, defined in the component file):

```typescript
interface WebappChatCache {
  sessionId: string
  messages: MessagePublic[]
  cachedAt: number
}
```

### Scoping

The cache key includes `webappToken` so:
- `webapp_chat_token_A` and `webapp_chat_token_B` are fully independent
- Incognito mode has a fresh localStorage, giving a fresh session automatically

---

## 4. Security Architecture

- No secrets stored: `sessionId` is a UUID (not sensitive), messages are already visible to the viewer
- The webapp JWT (`webapp_access_token`) is already stored in localStorage by the existing auth flow — this change follows the same pattern
- No cross-origin access concerns: localStorage is same-origin only
- No TTL required: sessions are server-side durable; the background verify call handles stale detection
- If the webapp JWT expires and the user re-authenticates, they get a new `webapp_share_id` JWT. The cache from the old session remains but the background verify will find no active session (since the new JWT identifies a different viewer context) and clear it automatically

---

## 5. Frontend Implementation

### File Modified

**`frontend/src/components/Webapp/WebappChatWidget.tsx`**

#### 5.1 Cache Helper Functions

Add three pure functions at the module level (outside the component):

```
WEBAPP_CHAT_CACHE_PREFIX = "webapp_chat_"

getCacheKey(webappToken: string): string
  → returns `webapp_chat_${webappToken}`

readCache(webappToken: string): WebappChatCache | null
  → localStorage.getItem(getCacheKey(webappToken))
  → JSON.parse, validate shape, return typed object or null on any error

writeCache(webappToken: string, sessionId: string, messages: MessagePublic[]): void
  → JSON.stringify({ sessionId, messages, cachedAt: Date.now() })
  → localStorage.setItem(getCacheKey(webappToken))
  → Wrap in try/catch (quota exceeded)

clearCache(webappToken: string): void
  → localStorage.removeItem(getCacheKey(webappToken))
```

#### 5.2 Component Props Change

Add `webappToken: string` to `WebappChatWidgetProps`. It is already available at the call site in `$webappToken.tsx` (the route already receives `webappToken` and passes it to the widget).

Current props:
```
webappToken: string    ← already present
chatMode: "conversation" | "building"
agentName: string
```

No props change needed — `webappToken` is already in the props interface.

#### 5.3 Mount Effect — Restore from Cache

Add a `useEffect` that runs once on mount (empty dependency array):

```
On mount:
  1. Call readCache(webappToken)
  2. If cache is null → return (normal flow, load on first open)
  3. If cache has sessionId and messages:
     a. setSessionId(cache.sessionId)        — instant restore
     b. setMessages(cache.messages)          — instant restore
     c. setIsOpen(true)                      — open panel if session exists (optional — see UX note)
     d. Kick off background verify (async, not awaited directly):
        - Call loadExistingSession() which already handles the GET /sessions call
        - Modify loadExistingSession() to be cache-aware (see §5.4)
```

UX decision: Do NOT automatically open the chat panel on restore. The session is restored silently — the user will see their history when they click the FAB. The FAB badge (unread indicator) can be set to true if there are messages, signaling the session exists.

#### 5.4 Modify `loadExistingSession()`

Current behavior: fetches session from API and sets state.
New behavior: when called as a background verify (session already in state from cache), behave differently:

```
loadExistingSession(isBackgroundVerify: boolean = false):
  if isBackgroundVerify:
    skip setIsLoadingSession(true)   ← don't show spinner, session already rendered
  try:
    session = await GET /sessions
    if session && session.id:
      setSessionId(session.id)
      setIsStreaming(session.interaction_status === "running" || "pending_stream")
      await loadMessages(session.id)   ← this also updates cache via §5.5
    else:
      // No active session found — clear cache and reset
      clearCache(webappToken)
      setSessionId(null)
      setMessages([])
  catch:
    if isBackgroundVerify:
      // Silent failure — keep cached state, try again next open
      console.error(...)
    else:
      // Normal error handling (already exists)
```

#### 5.5 Write Cache on State Change

Add two `useEffect` hooks that write to cache whenever relevant state changes:

```
useEffect on [sessionId, messages]:
  if sessionId:
    writeCache(webappToken, sessionId, messages)
  else:
    // No session — don't write (don't persist empty state)
```

This covers all cases: after initial load, after each new message, after messages refresh post-streaming.

#### 5.6 Mount Trigger for Background Verify

The existing session-load effect is:
```typescript
useEffect(() => {
  if (!isOpen || sessionId || isLoadingSession) return
  loadExistingSession()
}, [isOpen])
```

This won't fire on mount if `sessionId` is already set from cache (because `sessionId` is truthy). Instead, track background verify state with a ref:

```
const backgroundVerifyDoneRef = useRef(false)

// On mount effect (after cache restore sets sessionId):
useEffect(() => {
  if (!sessionId || backgroundVerifyDoneRef.current) return
  backgroundVerifyDoneRef.current = true
  loadExistingSession(true)   ← background verify, no spinner
}, [sessionId])
```

Only runs once because of the ref guard. After first run it is a no-op.

#### 5.7 `ensureSession()` — No Change Required

`ensureSession()` checks `if (sessionId) return sessionId` so cached session ID is used automatically. No change needed.

#### 5.8 Clear Cache on Reset

If in the future a "reset session" button is added, it should call `clearCache(webappToken)`. Note in code that this is where clearing belongs. No reset button is in scope here.

---

## 6. State Management

### React State Flow (existing + new)

| State variable | Source after restore | Write to cache? |
|---|---|---|
| `sessionId` | From localStorage cache (instant) | Yes (via effect) |
| `messages` | From localStorage cache (instant) | Yes (via effect) |
| `isStreaming` | From background verify result | No (ephemeral) |
| `streamingEvents` | Never persisted | No |
| `hasUnread` | Set to true if cache has messages on mount | No |

### No React Query

The chat widget uses raw `fetch` calls (not React Query) — this is consistent with the existing implementation pattern. The cache helper functions use localStorage directly, matching the webapp's existing `webapp_access_token` storage approach.

---

## 7. Database Migrations

None. This is a frontend-only change.

---

## 8. Error Handling and Edge Cases

| Scenario | Handling |
|---|---|
| localStorage throws (quota exceeded on write) | `writeCache` wraps in try/catch, silently skips |
| localStorage throws (read error, malformed JSON) | `readCache` returns null, falls back to normal flow |
| Session exists in cache but is gone from backend | Background verify detects no active session, calls `clearCache` + resets state |
| Session exists in cache, backend session still valid but has new messages | `loadMessages()` fetches fresh messages and updates cache |
| Webapp JWT expired between refreshes | Auth flow in `$webappToken.tsx` handles re-auth; widget gets fresh props and empty cache (different `webappToken`) |
| Multiple tabs open with same `webappToken` | Both tabs share the same localStorage entry; last writer wins on cache. Sessions are server-scoped to `webapp_share_id` so both tabs share the same session naturally |
| `webappToken` changes (navigate to different webapp) | New component mount with different `webappToken` → different cache key → fresh state |
| Cache exists but `sessionId` is null/empty in cache object | `readCache` validates shape; returns null if invalid |

---

## 9. UI/UX Considerations

- **No loading spinner on refresh**: If cache exists, messages render immediately without any loading state. The background verify is transparent to the user.
- **FAB badge**: Set `hasUnread = true` on mount if cache has messages. This signals the user that a conversation exists, prompting them to open the panel.
- **Panel auto-open**: Do NOT auto-open on refresh. The user consciously navigated away; re-opening automatically would be intrusive.
- **Incognito = fresh session**: Works naturally — incognito has empty localStorage.
- **No "session restored" toast**: Silent restoration is cleaner. The presence of messages in the panel is self-evident.

---

## 10. Integration Points

- **`frontend/src/components/Webapp/WebappChatWidget.tsx`**: Only file changed.
- **`frontend/src/routes/webapp/$webappToken.tsx`**: No changes. Already passes `webappToken` as prop.
- **Backend `GET /webapp/{token}/chat/sessions`**: Used as-is for background verification.
- **No client regeneration needed**: No backend API changes.

---

## 11. Future Enhancements (Out of Scope)

- TTL-based cache expiry (e.g., clear cache after 7 days) — `cachedAt` field already stored for this
- "New conversation" button in the chat widget that calls `clearCache` and resets state
- Syncing across tabs via `BroadcastChannel` API or `storage` event listener
- Persisting streaming events (current: only persisted messages are cached)

---

## 12. Summary Checklist

### Frontend Tasks

- [ ] Add `WebappChatCache` interface type definition in `WebappChatWidget.tsx`
- [ ] Add `WEBAPP_CHAT_CACHE_PREFIX` constant
- [ ] Implement `getCacheKey(webappToken)` helper
- [ ] Implement `readCache(webappToken)` helper with try/catch and shape validation
- [ ] Implement `writeCache(webappToken, sessionId, messages)` helper with try/catch
- [ ] Implement `clearCache(webappToken)` helper
- [ ] Add mount `useEffect` to restore `sessionId` and `messages` from cache
- [ ] Set `hasUnread = true` on mount if restored messages exist
- [ ] Add `backgroundVerifyDoneRef` ref to guard single background verify run
- [ ] Add `useEffect` on `[sessionId]` to trigger background verify after cache restore
- [ ] Modify `loadExistingSession(isBackgroundVerify?: boolean)` to skip loading spinner on background verify and call `clearCache` + reset when no active session found
- [ ] Add `useEffect` on `[sessionId, messages]` to write cache whenever state changes
- [ ] Verify existing `isOpen` effect still works for non-cached initial open (guard: `!sessionId`)

### Testing Tasks

- [ ] Verify: chat messages appear immediately on page refresh without loading spinner
- [ ] Verify: new messages sent after refresh are appended and persisted
- [ ] Verify: incognito window shows empty chat (fresh session)
- [ ] Verify: invalidated server session (e.g., session deleted on backend) clears cache and shows empty state
- [ ] Verify: different `webappToken` values maintain independent localStorage caches
- [ ] Verify: localStorage write error (simulate quota) does not crash the widget
- [ ] Verify: FAB badge appears on refresh when previous messages exist
- [ ] TypeScript check: `cd frontend && npx tsc --noEmit 2>&1 | grep "WebappChatWidget" | head -20`

---

*Created: 2026-03-08*
