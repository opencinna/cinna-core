import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  UsersService,
  UserWorkspacesService,
  type UserPublic,
  type UserWorkspaceCreate,
  type UserWorkspacePublic,
  type UserWorkspacesPublic,
} from "@/client"
import { useState, useEffect, createContext, useContext, type ReactNode } from "react"
import { handleError } from "@/utils"
import useCustomToast from "./useCustomToast"
import { isLoggedIn } from "./useAuth"

// Two storage layers decide which workspace a browser tab shows:
//   - Global preference (localStorage): the user's last MANUAL pick. New tabs
//     start here. Written only by `switchWorkspace` (sidebar dropdown, and the
//     create / delete-active-workspace flows).
//   - Tab workspace (sessionStorage): what THIS tab shows. Written by every
//     switch, including auto-activation from a detail page
//     (`activateTabWorkspace`), so opening an entity of another workspace by
//     URL re-scopes only the tab it was opened in. Other open tabs are not
//     updated live.
// Boot resolution: tab value if present, else the global preference.
const WORKSPACE_STORAGE_KEY = "last_user_workspace_id"
const TAB_WORKSPACE_STORAGE_KEY = "tab_user_workspace_id"
// Stored in the tab layer for "Default pinned in this tab", so it is
// distinguishable from "this tab has no value of its own".
const TAB_DEFAULT_MARKER = "default"

const readGlobalWorkspaceId = (): string | null => {
  const value = localStorage.getItem(WORKSPACE_STORAGE_KEY)
  // Normalize empty string and "null" string to actual null for default workspace
  if (value === "" || value === "null") {
    return null
  }
  return value
}

const writeGlobalWorkspaceId = (workspaceId: string | null) => {
  if (workspaceId === null) {
    localStorage.removeItem(WORKSPACE_STORAGE_KEY)
  } else {
    localStorage.setItem(WORKSPACE_STORAGE_KEY, workspaceId)
  }
}

/** `undefined` = this tab has no value of its own; `null` = Default pinned. */
const readTabWorkspaceId = (): string | null | undefined => {
  const value = sessionStorage.getItem(TAB_WORKSPACE_STORAGE_KEY)
  if (value === null || value === "") return undefined
  return value === TAB_DEFAULT_MARKER ? null : value
}

const writeTabWorkspaceId = (workspaceId: string | null) => {
  sessionStorage.setItem(
    TAB_WORKSPACE_STORAGE_KEY,
    workspaceId ?? TAB_DEFAULT_MARKER,
  )
}

/** The workspace this tab boots into: tab value, else global preference. */
const getActiveWorkspaceId = (): string | null => {
  const tabValue = readTabWorkspaceId()
  return tabValue !== undefined ? tabValue : readGlobalWorkspaceId()
}

// Create context for shared workspace state
const WorkspaceContext = createContext<{
  activeWorkspaceId: string | null
  setActiveWorkspaceIdState: (id: string | null) => void
} | null>(null)

export const WorkspaceProvider = ({ children }: { children: ReactNode }) => {
  const [activeWorkspaceId, setActiveWorkspaceIdState] = useState<string | null>(
    getActiveWorkspaceId,
  )

  // Stale-id repair, once for the whole app: a workspace deleted elsewhere
  // (another tab, Settings) can leave this tab — or the global preference —
  // pointing at nothing, which would filter every list to empty. Drop the
  // stale tab value and fall back to the global preference, and if that is
  // stale too, to Default.
  const { data: workspacesData } = useQuery({
    queryKey: ["userWorkspaces"],
    queryFn: () => UserWorkspacesService.readWorkspaces(),
    enabled: isLoggedIn(),
  })
  useEffect(() => {
    if (!workspacesData || activeWorkspaceId === null) return
    const known = new Set(workspacesData.data.map((w) => w.id))
    if (known.has(activeWorkspaceId)) return
    sessionStorage.removeItem(TAB_WORKSPACE_STORAGE_KEY)
    let fallback = readGlobalWorkspaceId()
    if (fallback !== null && !known.has(fallback)) {
      writeGlobalWorkspaceId(null)
      fallback = null
    }
    setActiveWorkspaceIdState(fallback)
  }, [workspacesData, activeWorkspaceId])

  return (
    <WorkspaceContext.Provider
      value={{
        activeWorkspaceId,
        setActiveWorkspaceIdState,
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  )
}

const useWorkspace = () => {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const context = useContext(WorkspaceContext)

  if (!context) {
    throw new Error('useWorkspace must be used within WorkspaceProvider')
  }

  const { activeWorkspaceId, setActiveWorkspaceIdState } = context

  // Workspaces-enabled preference is stored on the user profile so the
  // setting follows the user across browsers and devices. Subscribe to
  // the cached `["currentUser"]` query (populated by useAuth) so this
  // hook re-renders when the toggle flips. Providing the same `queryFn`
  // here keeps React Query happy when this hook mounts before `useAuth`
  // (e.g. AppSidebar) — React Query dedupes by `queryKey`, so the fetch
  // still runs only once.
  const { data: currentUser } = useQuery<UserPublic | null, Error>({
    queryKey: ["currentUser"],
    queryFn: () => UsersService.readUserMe(),
    enabled: isLoggedIn(),
  })
  const workspacesEnabled = currentUser?.workspaces_enabled ?? false

  // The value to send as `userWorkspaceId` on read queries:
  //   - undefined → no filter (workspaces feature disabled)
  //   - ""        → default workspace (NULL on backend)
  //   - <uuid>    → specific workspace
  const workspaceFilter: string | undefined = workspacesEnabled
    ? (activeWorkspaceId ?? "")
    : undefined

  // Fetch all user workspaces
  const { data: workspacesData } = useQuery({
    queryKey: ["userWorkspaces"],
    queryFn: () => UserWorkspacesService.readWorkspaces(),
  })

  const workspaces = workspacesData?.data || []
  const workspacesLoaded = workspacesData !== undefined

  // Get the currently active workspace object
  const activeWorkspace: UserWorkspacePublic | "default" | null =
    activeWorkspaceId === null
      ? "default"
      : workspaces.find((w) => w.id === activeWorkspaceId) || null

  // Manual switch (sidebar dropdown, create / delete flows): pins this tab
  // AND becomes the global preference new tabs start from. Never navigates —
  // the dropdown decides that (see SidebarWorkspaceSwitcher).
  const switchWorkspace = (workspaceId: string | null) => {
    writeGlobalWorkspaceId(workspaceId)
    writeTabWorkspaceId(workspaceId)
    setActiveWorkspaceIdState(workspaceId)
  }

  // Auto-activation (an entity's detail page): this tab only. The global
  // preference is left alone so other / new tabs are unaffected.
  const activateTabWorkspace = (workspaceId: string | null) => {
    writeTabWorkspaceId(workspaceId)
    setActiveWorkspaceIdState(workspaceId)
  }

  // Toggle whether workspace filtering is applied to UI queries.
  // Persisted on the user profile via PATCH /users/me; the cached
  // `["currentUser"]` query is updated optimistically so dependent UI
  // (sidebar switcher, workspaceFilter consumers) reflects the change
  // immediately, then invalidated once the server confirms.
  const setWorkspacesEnabledMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      UsersService.updateUserMe({ requestBody: { workspaces_enabled: enabled } }),
    onMutate: async (enabled) => {
      await queryClient.cancelQueries({ queryKey: ["currentUser"] })
      const previous = queryClient.getQueryData<UserPublic | null>(["currentUser"])
      if (previous) {
        queryClient.setQueryData<UserPublic>(["currentUser"], {
          ...previous,
          workspaces_enabled: enabled,
        })
      }
      return { previous }
    },
    onError: (_err, _enabled, ctx) => {
      if (ctx?.previous) {
        queryClient.setQueryData(["currentUser"], ctx.previous)
      }
      showErrorToast("Failed to update workspaces preference")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })

  const setWorkspacesEnabled = (enabled: boolean) => {
    setWorkspacesEnabledMutation.mutate(enabled)
  }

  // Create new workspace
  const createWorkspaceMutation = useMutation({
    mutationFn: (data: UserWorkspaceCreate) =>
      UserWorkspacesService.createWorkspace({ requestBody: data }),
    onSuccess: (newWorkspace) => {
      // Seed the cache first: the provider's stale-id repair checks the
      // active id against this list and would drop an id it doesn't contain.
      queryClient.setQueryData<UserWorkspacesPublic>(["userWorkspaces"], (old) =>
        old
          ? { data: [...old.data, newWorkspace], count: old.count + 1 }
          : { data: [newWorkspace], count: 1 },
      )
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
      // Automatically switch to the newly created workspace
      switchWorkspace(newWorkspace.id)
    },
    onError: handleError.bind(showErrorToast),
  })

  // Delete workspace
  const deleteWorkspaceMutation = useMutation({
    mutationFn: (workspaceId: string) =>
      UserWorkspacesService.deleteWorkspace({ workspaceId }),
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
      // If deleted workspace was active, switch to default
      if (activeWorkspaceId === deletedId) {
        switchWorkspace(null)
      }
    },
    onError: handleError.bind(showErrorToast),
  })

  // Update workspace
  const updateWorkspaceMutation = useMutation({
    mutationFn: ({
      workspaceId,
      data,
    }: {
      workspaceId: string
      data: { name: string }
    }) =>
      UserWorkspacesService.updateWorkspace({
        workspaceId,
        requestBody: data,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
    },
    onError: handleError.bind(showErrorToast),
  })

  return {
    workspaces,
    workspacesLoaded,
    activeWorkspace,
    activeWorkspaceId,
    workspaceFilter,
    workspacesEnabled,
    setWorkspacesEnabled,
    switchWorkspace,
    activateTabWorkspace,
    createWorkspaceMutation,
    deleteWorkspaceMutation,
    updateWorkspaceMutation,
  }
}

export { getActiveWorkspaceId }
export default useWorkspace
