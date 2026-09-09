---
feature: frontend_patterns
domain: development
one_liner: "Reference for frontend development patterns: component conventions, hooks, and TanStack Router and Query usage."
primary_label: reference
---
# Frontend Development - LLM Quick Reference

## Toast Notifications
- **USE**: `useCustomToast` hook from `@/hooks/useCustomToast`
- **DO NOT USE**: `@/hooks/use-toast` (does not exist)
- Library: `sonner` (not shadcn/ui toast)
```tsx
import useCustomToast from "@/hooks/useCustomToast"
const { showSuccessToast, showErrorToast } = useCustomToast()
showSuccessToast("Success message")
showErrorToast("Error message")
```

## API Client
- **NEVER manually edit** files in `src/client/`
- Auto-generated from backend OpenAPI spec
- Regenerate after backend changes: `bash scripts/generate-client.sh`
- Import services: `import { UsersService, AgentsService } from "@/client"`

## TanStack Query Patterns
```tsx
// Query
const { data } = useQuery({
  queryKey: ["key"],
  queryFn: () => ServiceName.methodName(),
})

// Mutation
const mutation = useMutation({
  mutationFn: (data) => ServiceName.create({ requestBody: data }),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["key"] })
    showSuccessToast("Success")
  },
  onError: () => showErrorToast("Error"),
})
```

## Routing
- File-based routing in `src/routes/`
- Protected routes: `src/routes/_layout/` directory
- Route guard pattern:
```tsx
export const Route = createFileRoute("/_layout/path")({
  component: Component,
  beforeLoad: async () => {
    if (!isLoggedIn()) throw redirect({ to: "/login" })
  },
})
```

## Auth
- Hook: `useAuth()` from `@/hooks/useAuth`
- Returns: `{ user, loginMutation, logoutMutation }`
- Access token stored in localStorage: `access_token`
- Check login: `isLoggedIn()` utility function

### Global 401/403 handling (`main.tsx handleApiError`)
Wired into the QueryClient's `QueryCache` **and** `MutationCache` `onError`, so it
sees every failed `useQuery` / `useMutation` in the app.

On a 401/403 it does **not** log the user out immediately — it first confirms the
session is actually dead by probing `LoginService.testToken()` (the same probe
`useAuth.ensureSessionValid` uses), and only then clears `access_token` and
redirects to `/login?redirect=<here>`. Concurrent failures share one in-flight
probe; a network/5xx probe result is treated as inconclusive and keeps the user
signed in.

**Why:** 401/403 is overloaded. Besides "your token is invalid" it also covers
"you may not do this" (role gates such as `require_developer`, which returns 403)
and routes reporting a *third-party* auth failure. Backend routes must not use
401/403 for anything other than the caller's own credentials — see
`agent_git_versioning_tech.md` for the git-versioning bug this caused, where a
rejected deploy key logged the user out mid-connect.

## Component Libraries
- UI: shadcn/ui components from `@/components/ui/`. Adding one: run `npx shadcn@latest add <name>` **from `frontend/`** (it is interactive). The generated file imports from the `radix-ui` umbrella package already in `package.json`, but the CLI has emitted `import { cn } from "cn"` and npm-installed a junk `cn` package — repoint to `@/lib/utils`, drop `"use client"`, and check `package.json` is unchanged before moving on.
- Radix `Toggle` / `ToggleGroupItem` write `data-state` **before** spreading props, so a `<TooltipTrigger asChild>` wrapper overwrites it and every `data-[state=on]:` class goes dead. Style the on-state off `aria-pressed:` instead (see `Admin/AccessPolicy/CompanyAiCredentialRow.tsx`).
- Forms: `react-hook-form` + `zod` validation
- Icons: lucide-react

## State Management
- **Primary**: TanStack Query (no Redux/Zustand)
- **Auth state**: Managed by TanStack Query with `["currentUser"]` key
- **Local state**: React useState

## Common Patterns
- User settings components: `src/components/UserSettings/`
- Settings page uses Tabs component with config array
- Always invalidate queries after mutations
- Use `queryClient.invalidateQueries()` for cache updates

## Compact List Row Pattern

The house pattern for a list of entities inside a Card or on a manage-list route. **Composition rules** (how many rows, which actions are visible, when to use a dialog vs a route) are defined in [ui_ux_guidelines.md](ui_ux_guidelines.md) — §2 "Row anatomy", "Row state", "Row flags", "Row height", "Row actions", "Toggles on rows", "List inside a card", "Inline forms", and pattern P3. This section only records the wiring skeleton.

### Structure

**Container**: Card (in a tab grid) or a manage-list route (`DataTable`)
- `CardHeader` with an icon + noun title, a one-line description, and the primary "Add …" `Button size="sm"` (`Plus` icon) on the title row
- Up to **5** rows in `CardContent`, wrapped in `ListRowGroup` (or rendered through `Common/PreviewList.tsx`, which supplies the group and the four states); more → "Show all (N)" link to a route or full-height `Sheet` (guideline P5)
- Empty state: one sentence + the Add button
- Edit / create `Dialog` rendered once at the bottom, controlled by `editingItem` state

### Row layout

Use `Common/ListRow.tsx`. **Do not hand-roll a row `div`** — the border, the ordering and the state rendering are the pattern, not the caller's choice.

```tsx
import { ListRow, ListRowGroup, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { OnOffToggle } from "@/components/Common/OnOffToggle"

<ListRowGroup>
  {items.map((item) => (
    <ListRow
      key={item.id}
      muted={!item.enabled}                                  // dims an inactive row
      status={{ tone: item.enabled ? "on" : "off", label: "Enabled" }}
      icon={/* optional h-6 w-6 identity tile */}
      title={item.name}
      badges={/* ≤ 1 Badge, only for a short word people scan by */}
      meta="every day · next 09:00"                          // omit → the row is one line high
      flags={
        <>
          <RowFlag icon={Terminal} label="Script trigger" tone="neutral" />
          <RowInfo facts={["Created 3 Mar", "Never used"]} />
        </>
      }
    >
      <OnOffToggle checked={item.enabled} onChange={…} label={item.name} />
      <RowActionsMenu label={`the schedule ${item.name}`} disabled={isPending}>
        <DropdownMenuItem …>…</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" …>…</DropdownMenuItem>
      </RowActionsMenu>
    </ListRow>
  ))}
</ListRowGroup>
```

Rendering order is fixed by the component: **dot → tile → title/meta → flags → children**. `children` is the action cluster: at most one ghost icon button (`h-7 w-7`, icon `h-3.5 w-3.5`, in a `Tooltip`), the `OnOffToggle` when toggling is the row's purpose, then the `⋯` menu.

Hover-reveal variant (dense lists, a lone destructive control): put `opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity` on that control — `ListRow` always carries `group`.

**Pending is per row**, not per list: `disabled={mutation.isPending && mutation.variables?.id === item.id}`. A shared `useMutation` observer describes only its latest call, so `isPending` alone freezes every row.

**Confirms belong to the row.** A `DropdownMenuItem` unmounts on select, so an `AlertDialog` whose trigger is nested in one loses its pending state. Either the row component owns its own confirm (`ScheduleRow`, `HandoverRow`, `BundleRevisionRow`), or the menu item sets a list-level `deleteTarget` that one confirm reads (`ServerChannelsCard`, `GuestShareCard`).

### Actions

| Action | UI | Pattern |
|--------|-----|---------|
| Create | "Add …" `Button size="sm"` in the header | Opens the create Dialog (type picker first when the form depends on a type — guideline P4/P6) |
| Open / Run / Copy | The single inline ghost icon button | Direct action; Copy toggles `Copy`/`Check` with `setCopiedId` + `setTimeout` reset |
| Edit | Menu item | Opens the edit Dialog populated from the row |
| Enable / Disable | Menu item — or an inline `OnOffToggle` when toggling is what the list is *for* | Direct mutation, toast on result, no confirmation. Never a `Switch` on a row |
| Set default | Menu item | Direct mutation, toast on result, no confirmation |
| Delete | Last menu item, `variant="destructive"` | `AlertDialog` naming the entity, then mutation. Kept open while the request is in flight so the pending state has somewhere to live |

### Edit Dialog

Separate `Dialog` controlled by `editingItem` state:
- `handleEditOpen(item)`: sets `editingItem` + populates the form
- `handleEditSave()`: sends only changed fields
- On success: close, `invalidateQueries`, toast

Inline editing in the row is allowed only for a **single-field** quick edit (rename) and only one row at a time; a multi-field form is always a Dialog.

### State management

- `useState` for `editingItem`, dialog open flags, `copiedId`
- `useMutation` per action with `onSuccess` → `queryClient.invalidateQueries`
- `isError` rendered as an `Alert` with Retry — separately from the empty state

### Reference implementations

- `frontend/src/components/Admin/UserActionsMenu.tsx` — the per-row overflow menu (invitation actions first, Edit, Delete)
- `frontend/src/components/UserSettings/AICredentials/SdkPreferencesCard.tsx` — "Default SDK Preferences" summary rows + `SDKModeEditDialog` (guideline P2)
- `frontend/src/components/Credentials/AddCredential.tsx` — type picker → detail route (guideline P4)

- `frontend/src/components/Agents/HandoverRow.tsx` / `ScheduleRow.tsx` — the two P3 rows (menu, off-only badge, `AlertDialog` confirm) over `Common/PreviewList.tsx`

Files that still implement the older always-visible-icons variant (`WebappShareCard.tsx`, `McpConnectorsCard.tsx`) are migrated when touched.

## Environment Variables
- Accessed via `import.meta.env.VITE_*`
- API URL: `import.meta.env.VITE_API_URL`

## Styling
- Tailwind CSS
- Theme: Light/dark mode support via shadcn/ui
- Use `className` prop with Tailwind utilities
- Dynamic classes: Use template literals with full class names (Tailwind JIT)
```tsx
const colorPreset = getColorPreset(agent.ui_color_preset)
<div className={`rounded-lg p-3 ${colorPreset.iconBg}`}>
```
- Visual **skins** (a second, orthogonal theming axis layered on top of light/dark mode) are pure CSS-variable overlays keyed off `data-skin` on `<html>` — see [Theming — tech](../../application/theming/theming_tech.md#adding-a-new-skin) for the token tiers and the step-by-step "adding a skin" recipe. Never hardcode a status/accent color that should re-tint under a skin (e.g. `bg-emerald-500`) — use the semantic token (`bg-primary`) instead.

## Dialog/Modal Pattern
```tsx
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

const [isOpen, setIsOpen] = useState(false)

<Dialog open={isOpen} onOpenChange={setIsOpen}>
  <DialogContent className="sm:max-w-md">
    <DialogHeader>
      <DialogTitle>Title</DialogTitle>
      <DialogDescription>Description</DialogDescription>
    </DialogHeader>
    {/* Content */}
  </DialogContent>
</Dialog>
```

A dialog whose body is **data-driven** (fact lists, a list of items, a document pane) needs three more things, all learned from the addons detail dialog (2026-09-09):

```tsx
<DialogContent
  // grows with the data → scrolls inside the viewport; `DialogContent` is a CSS
  // grid, so `[&>*]:min-w-0` stops one long word or `truncate` row widening it
  className="max-h-[85vh] overflow-x-hidden overflow-y-auto sm:max-w-2xl [&>*]:min-w-0"
  // Radix focuses the first focusable element on open; if that control has a
  // Tooltip (a click-to-copy value) the tooltip opens over nothing — focus the
  // body instead. Tab still reaches every control.
  onOpenAutoFocus={(e) => {
    e.preventDefault()
    ;(e.currentTarget as HTMLElement | null)?.focus()
  }}
>
```

Long prose inside gets `break-words`; a wide table or code block scrolls in its own `overflow-x-auto` pane, never the dialog.

## Timestamps
The backend serialises naive UTC (`2026-09-09T10:00:00`, no `Z`). `new Date()` reads that as **local** time, so any label built from it is off by the viewer's UTC offset. Never `formatDistanceToNow(new Date(x))` on a wire value.

```tsx
import { RelativeTime, formatRelativeTimestamp, parseTimestamp } from "@/components/Common/RelativeTime"

<RelativeTime timestamp={link.created_at} showTooltip />        // rendered: "3 hours ago", full local date on hover
const ago = formatRelativeTimestamp(link.created_at)            // string, for a RowInfo fact or a description line
const date = parseTimestamp(x)                                   // Date | null — the one parser
```

## Click to copy
A value that is already on screen and exists to be pasted (a commit hash, an id) is **its own** copy control — no second boxed input:

```tsx
<Tooltip>
  <TooltipTrigger asChild>
    <button type="button" className="max-w-full cursor-pointer truncate font-mono text-sm hover:underline"
      aria-label="Copy the commit hash" onClick={() => copy(hash)}>
      {hash}
    </button>
  </TooltipTrigger>
  <TooltipContent side="top" className="text-xs">Click to copy</TooltipContent>
</Tooltip>
```
`copy` wraps `navigator.clipboard.writeText` in try/catch with `showSuccessToast` / `showErrorToast` — the clipboard is undefined outside a secure context and rejects on denied permission. `Common/CopyableValue` (label + boxed value + button) is for values the user reads *as a field* and that are not otherwise shown.

## Wire strings
An absent string arrives as `""` as often as `null`. Display fallbacks use `||`, not `??`: `` `v${link.latest_version ?? "latest"}` `` printed "v" for a plugin whose manifest had no version.

## Utilities Pattern
- Shared constants/helpers: `src/utils/` directory
- Export types and functions
- Example: `src/utils/colorPresets.ts` for color configuration
```tsx
export type ColorPreset = "slate" | "blue" | ...
export const getColorPreset = (preset: string | null | undefined) => { ... }
```

## Tab Components
- Use `HashTabs` component for tabbed interfaces
- Location: `@/components/Common/HashTabs`
```tsx
const tabs = [
  { value: "tab1", title: "Tab 1", content: <Component1 /> },
  { value: "tab2", title: "Tab 2", content: <Component2 /> },
]
<HashTabs tabs={tabs} defaultTab="tab1" />
```

## Workspace Management

### State Access
- **Hook**: `useWorkspace()` from `@/hooks/useWorkspace`
- **Returns**: `{ activeWorkspaceId, activeWorkspace, switchWorkspace, workspaces, ... }`
- **Important**: Uses React Context - all components share same workspace state

### List Pages with Workspace Filtering
**Problem**: When user switches workspaces, list pages must refresh with new data.

**Solution**: React `key` prop pattern + Context for state sharing

**Implementation Pattern**:
```tsx
function MyListPage() {
  const { activeWorkspaceId } = useWorkspace()

  // Query key MUST include activeWorkspaceId
  const { data } = useQuery({
    queryKey: ["myEntities", activeWorkspaceId],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey  // Read from queryKey, not closure
      return MyService.list({
        userWorkspaceId: workspaceId ?? "",  // Empty string = default workspace
      })
    },
  })

  // Key prop forces remount when workspace changes
  return (
    <div key={activeWorkspaceId ?? 'default'}>
      {/* Component content */}
    </div>
  )
}
```

**Why This Pattern**:
- **React Context**: Ensures all components see workspace state changes (prevents desync)
- **Key prop**: Forces component unmount/remount when workspace changes → fresh queries
- **Query key includes workspace**: Separates cache by workspace
- **Read from queryKey param**: Avoids closure issues with stale workspace values
- **Empty string convention**: Backend interprets `""` as default workspace filter

**Required for pages**: `/`, `/agents`, `/credentials`, `/sessions`, `/activities`

### Detail Pages with Workspace Context
**Problem**: When viewing an entity detail page (e.g., agent page) and switching workspaces, that entity likely doesn't exist in the new workspace.

**Solution**: Automatic redirect to index

**Implementation**: Handled in `useWorkspace` hook automatically - detail pages redirect to `/` on workspace switch

**List of workspace-aware pages**:
- `/` - index/dashboard
- `/agents` - agents list
- `/credentials` - credentials list
- `/sessions` - sessions list
- `/activities` - activities list

**All other pages**: Auto-redirect to index on workspace switch

### Creating Entities in Active Workspace
```tsx
const { activeWorkspaceId } = useWorkspace()

const createMutation = useMutation({
  mutationFn: (data) => MyService.create({
    requestBody: {
      ...data,
      user_workspace_id: activeWorkspaceId,  // Assign to active workspace
    }
  }),
})
```

## Navigation History & Back Button

Detail pages use a tab-scoped navigation history stack for intuitive Back button behavior. Instead of hardcoding a destination (e.g., always going to `/agents`), the Back button returns to the actual previous page.

### Architecture

Two hooks in `src/hooks/useNavigationHistory.ts`:

- **`useNavigationTracker()`** — called once in `_layout.tsx`. Records every route change into a `sessionStorage` stack (max 50 entries). Since `sessionStorage` is per browser tab, different tabs maintain independent histories.
- **`useNavigationHistory()`** — returns `goBack(fallback)`. Pops the most recent URL from the stack. If the stack is empty (e.g., direct URL access), navigates to `fallback`.

When `goBack` is called, the current page is **not** pushed back onto the stack, so repeated Back presses walk through the history linearly.

### Usage in Detail Pages

Every detail page with a Back button follows this pattern:

```tsx
import { useNavigationHistory } from "@/hooks/useNavigationHistory"

function DetailPage() {
  const { goBack } = useNavigationHistory()

  const handleBack = () => {
    goBack("/entities")  // fallback if history is empty
  }

  // Use handleBack in the header Back button
}
```

### Adding a Back Button to a New Page

1. Import `useNavigationHistory` and call `goBack(fallback)` in your back handler
2. Choose a sensible `fallback` — typically the entity's index page (e.g., `/agents`, `/tasks`)
3. No changes needed in `_layout.tsx` — the tracker is already running there

### Behavior Examples

| Navigation path | Back button goes to |
|----------------|-------------------|
| `/tasks` → `/task/123` | `/tasks` |
| `/task/456` → `/task/456/subtask/789` → `/session/abc` | `/task/456/subtask/789`, then `/task/456` |
| Direct URL to `/agent/xyz` (no history) | `/agents` (fallback) |
| Tab A and Tab B open different pages | Each tab has independent history |
