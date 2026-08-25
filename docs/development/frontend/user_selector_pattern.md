# User Selector Pattern (shared)

## Purpose

A single, reusable way to let a user pick *other users* to share something with — credentials, direct/agent-to-agent MCP connector ACLs, identity bindings, bundle access grants, Agent REST API scopes. One component (`UserAllowlistPicker`) backed by one endpoint (`GET /users/search`), so every sharing surface behaves identically and works for **non-admin** owners (agent-developers), not just admins.

## The component

`frontend/src/components/Common/UserAllowlistPicker.tsx`

- Renders: a search `Input`, a results dropdown, and the current selection as removable **pills**.
- Search is **server-side** via `UsersService.searchUsers({ q, limit, includeSelf })` under React Query key `["user-search", <debounced query>, includeSelf]`; fires only when the query is ≥ 2 chars. The current user is **excluded server-side by default**; pass `includeSelf` to include them (see below).
- The query is **debounced ~250ms** — keystrokes update an immediate `query` state, but the React Query key tracks a `debouncedQuery` that lags behind, so a request fires only after typing pauses (no per-keystroke fetch).
- The dropdown shows a **"Searching…"** state for the whole window where an answer isn't ready yet — debounce-pending (`query !== debouncedQuery`), request in flight, or data not yet loaded. This avoids a "No matching users." flash in the gap between a keystroke and the debounced fetch starting.
- The results dropdown renders as a **portalled Radix `Popover`** anchored to the input (`PopoverAnchor asChild` around the `Input`; content width follows `--radix-popover-trigger-width`). Because the list lives in a portal rather than inside the picker's own DOM, a clipping or scrolling host — a dialog body with `max-h-… overflow-y-auto`, a card with `overflow-hidden` — can neither cut the results off nor gain an inner scrollbar because of them, and Radix collision detection flips the list above the input when there is no room below. Host containers still don't jump in height as the user types.
  - Open state is derived (`query ≥ 2 chars && !dismissed`), not user-toggled: Escape / click-outside sets `dismissed` (keeping what was typed), and the next keystroke re-opens. `onOpenAutoFocus` / `onCloseAutoFocus` are prevented so the caret never leaves the search input, and `onInteractOutside` is ignored for events inside the input itself (the input is the *anchor*, so Radix would otherwise treat clicking one's own query as an outside interaction).
- The component no longer loads the full user list, so **pill labels come from `fallbackLabel`** on each selected item. Callers must supply it (a name or email); when omitted the pill renders the literal text "Unknown user".

Props: `selected: UserAllowlistSelectedItem[]` (`{ id, userId, fallbackLabel? }` — `id` is the caller's delete key e.g. share/assignment id, `userId` is the platform user id used to filter results), `onAdd(user)`, `onRemove(item)`, `isAdding?`, `isRemoving?`, `searchPlaceholder?`, `emptyHint?`, `label?` (ReactNode | null to hide), `enabled?` (gate the fetch, pass the dialog-open boolean), `excludeUserIds?` (extra ids filtered from results without rendering pills), `includeSelf?` (include the current user in results — off by default).

### Self-selection (`includeSelf`)

Most pickers share *something with someone else*, so self-selection is meaningless and the current user is filtered out server-side. Some surfaces, however, legitimately target the owner themselves — the Agent REST API **Access & Scopes** card is the canonical case: the producer's owner is frequently the calling user (when one of their own agents calls the producer API) and must be able to assign scopes to themselves. Those pickers pass `includeSelf`, which forwards `include_self=true` to the endpoint so the requester is no longer excluded. Default behaviour (and every share/assignment picker) is unchanged.

## The endpoint

`GET /api/v1/users/search?q=&limit=` — see [Credential Sharing tech](../../agents/agent_credentials/credential_sharing_tech.md#user-search-for-sharing-pickers) for the full contract.

- Available to **any authenticated user** (contrast with admin-only `GET /users/`).
- Returns a minimal `UsersSearchPublic` projection (`UserSearchResult`: `id` / `email` / `full_name`) — never the full `UserPublic`.
- Min query length 2, `limit` clamped 1-25, LIKE wildcards escaped. Excludes the requester by default; `include_self=true` keeps them in results (passes `exclude_user_id=None` to the service).
- Backend: `UserService.search_users()` (`backend/app/services/users/user_service.py`), route in `backend/app/api/routes/users.py` (declared before `/{user_id}`).

## Callers

- `frontend/src/components/Credentials/CredentialSharing.tsx` — credential direct sharing (pills = existing `CredentialShare` rows; `onAdd` shares by email, `onRemove` revokes).
- `frontend/src/components/Agents/McpConnectorsCard.tsx` — the direct-connector `allowed_user_ids` ACL and the agent-to-agent connector ACL. (The App MCP route and identity options were removed from this dialog in Phase 5 of the channels & identity unification; identity binding pickers live on `IdentityServerCard.tsx` below.)
- `frontend/src/components/UserSettings/IdentityServerCard.tsx` — identity binding edit picker.
- `frontend/src/components/Agents/BundlePermissionsAddUserModal.tsx` — the Bundle tab's unified Permissions management add/edit dialog (single-user selection; the dialog body scrolls, hence the portalled results list).
- `frontend/src/components/Agents/AgentApiAccessScopesCard.tsx` — Agent REST API per-user scope grants; passes `includeSelf` (owner-grants-self is valid here).
- `frontend/src/components/Credentials/AgentApiKeyDialog.tsx` — external `agent_api` key subject binding.
- `frontend/src/components/Admin/LlmProviders/ManagedCredentialDialog.tsx` — managed AI credential target-user membership.

## Pill labels for edit pickers

When a picker shows *existing* assignments (edit dialogs), the assignment record must carry the user's display info so the pill renders without a user-list lookup:

- `IdentityBindingAssignmentPublic` carries `target_user_name` / `target_user_email`.
- `CredentialSharePublic` carries `shared_with_email` (+ `shared_with_user_id`).

## Exception

`frontend/src/routes/_layout/admin/users.tsx` intentionally keeps `UsersService.readUsers` (`GET /users/`) — it is the admin user-management list, not a sharing picker, and must show all users with full detail.

## Integration Points

- [Credential Sharing](../../agents/agent_credentials/credential_sharing.md)
- [Identity MCP Server](../../application/identity_mcp_server/identity_mcp_server.md)
- [Agent Bundles](../../agents/agent_bundles/agent_bundles.md)
