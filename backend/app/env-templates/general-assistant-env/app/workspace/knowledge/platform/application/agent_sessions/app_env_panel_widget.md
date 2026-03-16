# Environment Panel Widget

## Widget Purpose

The Environment Panel is a collapsible right-side panel in the session view that gives users access to the agent's workspace artifacts — files, scripts, logs, docs, and uploads generated during agent execution. It also provides quick access to credential management without leaving the session.

Triggered by the **"App" button** in the session header. The panel overlays the message list but never covers the message input footer, so conversations can continue while the panel is open.

See [Agent Sessions](./agent_sessions.md) for how the panel fits into the session page layout.

## User Flow

1. User runs an agent session; agent generates workspace files (CSV reports, scripts, logs, etc.)
2. Agent notifies user that files are ready (or user opens panel to check)
3. User clicks **"App"** button in session header (Package icon)
4. Panel slides in from the right at 384px width; defaults to **Files** section
5. User sees a tree of workspace folders and files
6. User expands a folder by clicking its name — chevron toggles and children appear
7. User hovers over a file — extension, size, and download button fade in
8. User clicks download → browser receives the file (or ZIP archive for folders)
9. User switches to **Logs** via the dropdown menu at the top of the panel
10. User clicks **Expand** (Maximize2 icon) to widen panel to 768px for better readability
11. User clicks **"App"** button again to close the panel (no close button inside the panel)

**SQLite flow:**
- User sees a `.sqlite` file in Files section
- User clicks to expand it — panel fetches table list from backend
- User clicks a table → opens `/environment/{envId}/database?path=...&table=...` in a new tab
- See [Remote Database Viewer](../../agents/agent_file_management/remote_database_viewer.md)

**Credentials flow:**
- User selects **Credentials** from dropdown
- User checks/unchecks credentials to share or unshare them with the agent in real time

**Guest flow:**
- Guest users see the same panel but with Credentials tab hidden and database viewer disabled
- File/folder download and workspace tree work normally for guests
- See [Guest Sharing](../../agents/agent_sharing/guest_sharing.md)

## Component Structure

**Main component:** `frontend/src/components/Environment/EnvironmentPanel.tsx`
- Sticky absolute positioning within the message list container (relative parent)
- Width: 384px default, 768px expanded; transitions via `transition-all duration-200`
- Does not cover message input footer (positioned within message list only)
- `isOpen` prop controls visibility; toggled by "App" button in session route

**Subcomponents:**
- `frontend/src/components/Environment/TabHeader.tsx` — Dropdown navigation + expand/shrink toggle. Accepts `hideCredentials` prop for guest mode
- `frontend/src/components/Environment/WorkspaceTabContent.tsx` — Reusable tab content wrapper for all workspace sections (Files, Scripts, Logs, Docs, Uploads). Passes `isGuest` to `TreeItemRenderer`
- `frontend/src/components/Environment/TreeItemRenderer.tsx` — Recursive tree rendering for files, folders, and SQLite databases. `isGuest` prop gates database viewer and adapts file viewer URL
- `frontend/src/components/Environment/CredentialsTabContent.tsx` — Credential share/unshare UI with checkbox list
- `frontend/src/components/Environment/StateComponents.tsx` — Loading, error, empty, and no-environment states
- `frontend/src/components/Environment/FileIcon.tsx` — Type-specific file icons with color coding

**Utilities:**
- `frontend/src/components/Environment/types.ts` — `FileItem`, `FolderItem`, `TreeItem`, `DatabaseTableItem`, SQLite schema types
- `frontend/src/components/Environment/utils.ts` — `convertFileNodeToTreeItem()`, `formatFileSize()`, `formatDate()`, `getFileExtension()`

**Integration points:**
- `frontend/src/routes/_layout/session/$sessionId.tsx` — renders `EnvironmentPanel`, passes `envPanelOpen` state, `env_id` resolved from `agent_usage_intent`
- `frontend/src/routes/guest/$guestShareToken.tsx` — same panel rendered in guest layout; `isGuest` detected via `useGuestShare()` hook

## Navigation Structure

The dropdown menu at the top of the panel selects the active section:

| Section | Content | Default |
|---------|---------|---------|
| **Files** | Reports, CSV, JSON, SQLite, caches | ✅ Yes |
| **Scripts** | Python/executable scripts created by agent | |
| **Logs** | Agent execution and debug logs | |
| **Docs** | Generated documentation (Markdown) | |
| **Uploads** | User-uploaded files | |
| **Credentials** | Agent credential sharing (hidden for guests) | |

**Layout:** Dropdown selector (flex-1) + Expand/Shrink button (Maximize2/Minimize2 icons). No visual separator between menu and content list.

## File/Folder Tree Behavior

**Folders:** Chevron + Folder icon (blue) + Name. Hover reveals size + download button. Click name to expand/collapse. Click download to download as ZIP.

**Files:** Icon + Basename. Hover fades in extension (dimmed), size (dimmed), and download button. Click download for direct file download.

**Indentation:** Folders at `12px × level`; files at `12px × level + 24px`.

**Download implementation:** `handleDownload()` in `EnvironmentPanel.tsx`. Axios intercepts with `responseType: 'blob'`, triggers browser download via blob URL. For folders: streamed as ZIP from backend. See [Agent File Management](../../agents/agent_file_management/agent_file_management.md) for file storage and transfer details.

**SQLite database browser:** Expand `.sqlite` or `.db` files to see tables and views inline. Tables open the database viewer in a new tab (owner-only). See [Remote Database Viewer](../../agents/agent_file_management/remote_database_viewer.md).

**Icon color coding** (`FileIcon.tsx`): CSV (green), JSON (blue), TXT (gray), PY (purple), LOG (yellow), MD (indigo), SQLite (database icon), default (gray).

## State Management

State lives in `EnvironmentPanel.tsx`:

| State | Type | Purpose |
|-------|------|---------|
| `activeTab` | string | Current section: `files / scripts / logs / docs / uploads / credentials` |
| `expandedFolders` | `Set<string>` | Expanded folder paths (also used for expanded SQLite files); path-based keys prevent conflicts |
| `isWidePanelMode` | boolean | Panel width toggle: `false` = 384px, `true` = 768px |
| `databaseTables` | `Record<string, {tables, loading, error}>` | SQLite file path → fetched table list |

**React Query:**
- `useQuery` — workspace tree (`staleTime: 5s`) and credentials list
- `useMutation` — credential share (`AgentsService.addCredentialToAgent`) and unshare (`AgentsService.removeCredentialFromAgent`)

## API Interactions

All endpoints use the environment ID resolved from the session's active environment. See [Agent Environments](../../agents/agent_environments/agent_environments.md) for environment lifecycle and [Agent Environment Data Management](../../agents/agent_environment_data_management/agent_environment_data_management.md) for workspace directory structure.

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/v1/environments/{env_id}/workspace/tree` | `CurrentUserOrGuest` | Fetch full workspace tree (files, scripts, logs, docs, uploads sections) |
| `GET /api/v1/environments/{env_id}/workspace/download/{path}` | `CurrentUserOrGuest` | Download file or folder (ZIP for folders) |
| `GET /api/v1/environments/{env_id}/workspace/view-file/{path}` | `CurrentUserOrGuest` | Stream file content for viewer |
| `GET /api/v1/environments/{env_id}/workspace/database/{path}/tables` | `CurrentUser` (owner-only) | Fetch SQLite table/view list |
| `GET /api/v1/environments/{env_id}` | `CurrentUser` | Environment details (skipped for guests) |
| `GET /api/v1/agents/{agent_id}/credentials` | `CurrentUser` | Agent's linked credentials (skipped for guests) |
| `GET /api/v1/credentials` | `CurrentUser` | All user credentials for checkbox list (skipped for guests) |
| `POST /api/v1/agents/{agent_id}/credentials/{cred_id}` | `CurrentUser` | Share credential with agent |
| `DELETE /api/v1/agents/{agent_id}/credentials/{cred_id}` | `CurrentUser` | Unshare credential from agent |

Workspace directory structure (`/app/workspace/`):
- `files/` — Files section
- `scripts/` — Scripts section
- `logs/` — Logs section
- `docs/` — Docs section
- `uploads/` — Uploads section

This maps directly to the workspace layout defined in [Agent Environment Data Management](../../agents/agent_environment_data_management/agent_environment_data_management.md#workspace-directory-structure).

## Guest Mode

When rendered in a guest session, `isGuest=true` is propagated from `EnvironmentPanel` → `WorkspaceTabContent` → `TreeItemRenderer`.

**Disabled:**
- Credentials tab (hidden via `hideCredentials` prop on `TabHeader`)
- SQLite database viewer (table click is a no-op)
- Environment details query (no SDK footer)

**Adapted:**
- File viewer URL: `/guest/file-viewer?envId=...&path=...` instead of `/environment/{envId}/file` (authenticated route)
- Workspace tree and file download work normally (backend supports `CurrentUserOrGuest`)