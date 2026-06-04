# User Selector Pattern (shared)

## Purpose

A single, reusable way to let a user pick *other users* to share something with — credentials, App MCP routes, identity bindings, bundle access grants. One component (`UserAllowlistPicker`) backed by one endpoint (`GET /users/search`), so every sharing surface behaves identically and works for **non-admin** owners (agent-developers), not just admins.

## The component

`frontend/src/components/Common/UserAllowlistPicker.tsx`

- Renders: a search `Input`, a results dropdown, and the current selection as removable **pills**.
- Search is **server-side** via `UsersService.searchUsers({ q, limit })` under React Query key `["user-search", <debounced query>]`; fires only when the query is ≥ 2 chars. The current user is excluded server-side.
- The query is **debounced ~250ms** — keystrokes update an immediate `query` state, but the React Query key tracks a `debouncedQuery` that lags behind, so a request fires only after typing pauses (no per-keystroke fetch).
- The dropdown shows a **"Searching…"** state for the whole window where an answer isn't ready yet — debounce-pending (`query !== debouncedQuery`), request in flight, or data not yet loaded. This avoids a "No matching users." flash in the gap between a keystroke and the debounced fetch starting.
- The results dropdown renders as an **absolute-positioned popover** (`absolute … top-full z-50`, on `bg-popover`) anchored to the input inside a `relative` wrapper, so it overlays content below instead of reflowing — host containers (e.g. a settings card) don't jump in height as the user types. Caveat: a host that clips with `overflow-hidden` and little room below can clip the popover.
- The component no longer loads the full user list, so **pill labels come from `fallbackLabel`** on each selected item. Callers must supply it (a name or email); when omitted the pill renders the literal text "Unknown user".

Props: `selected: UserAllowlistSelectedItem[]` (`{ id, userId, fallbackLabel? }` — `id` is the caller's delete key e.g. share/assignment id, `userId` is the platform user id used to filter results), `onAdd(user)`, `onRemove(item)`, `isAdding?`, `isRemoving?`, `searchPlaceholder?`, `emptyHint?`, `label?` (ReactNode | null to hide), `enabled?` (gate the fetch, pass the dialog-open boolean).

## The endpoint

`GET /api/v1/users/search?q=&limit=` — see [Credential Sharing tech](../../agents/agent_credentials/credential_sharing_tech.md#user-search-for-sharing-pickers) for the full contract.

- Available to **any authenticated user** (contrast with admin-only `GET /users/`).
- Returns a minimal `UsersSearchPublic` projection (`UserSearchResult`: `id` / `email` / `full_name`) — never the full `UserPublic`.
- Min query length 2, `limit` clamped 1-25, excludes the requester, LIKE wildcards escaped.
- Backend: `UserService.search_users()` (`backend/app/services/users/user_service.py`), route in `backend/app/api/routes/users.py` (declared before `/{user_id}`).

## Callers

- `frontend/src/components/Credentials/CredentialSharing.tsx` — credential direct sharing (pills = existing `CredentialShare` rows; `onAdd` shares by email, `onRemove` revokes).
- `frontend/src/components/Agents/McpConnectorsCard.tsx` — App MCP route create + edit pickers, and the identity create + edit pickers.
- `frontend/src/components/UserSettings/IdentityServerCard.tsx` — identity binding edit picker.
- `frontend/src/components/Agents/AgentBundleTab.tsx` — bundle access grants.

## Pill labels for edit pickers

When a picker shows *existing* assignments (edit dialogs), the assignment record must carry the user's display info so the pill renders without a user-list lookup:

- `IdentityBindingAssignmentPublic` carries `target_user_name` / `target_user_email`.
- `AppAgentRouteAssignmentPublic` carries `user_email` / `user_full_name`, resolved in `app_agent_route_service.py:_assignment_to_public()`; lists are serialized via `app_agent_route_service.py:_assignments_to_public()`, which batch-resolves the assigned users in one query to avoid an N+1 per assignment.
- `CredentialSharePublic` carries `shared_with_email` (+ `shared_with_user_id`).

## Exception

`frontend/src/routes/_layout/admin/users.tsx` intentionally keeps `UsersService.readUsers` (`GET /users/`) — it is the admin user-management list, not a sharing picker, and must show all users with full detail.

## Integration Points

- [Credential Sharing](../../agents/agent_credentials/credential_sharing.md)
- [App MCP Server](../../application/app_mcp_server/app_mcp_server.md)
- [Identity MCP Server](../../application/identity_mcp_server/identity_mcp_server.md)
- [Agent Bundles](../../agents/agent_bundles/agent_bundles.md)
