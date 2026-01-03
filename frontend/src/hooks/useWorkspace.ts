import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  UserWorkspacesService,
  type UserWorkspaceCreate,
  type UserWorkspacePublic,
} from "@/client"
import { useState, useEffect } from "react"
import { handleError } from "@/utils"
import useCustomToast from "./useCustomToast"

const WORKSPACE_STORAGE_KEY = "last_user_workspace_id"

const getActiveWorkspaceId = (): string | null => {
  return localStorage.getItem(WORKSPACE_STORAGE_KEY)
}

const setActiveWorkspaceId = (workspaceId: string | null) => {
  if (workspaceId === null) {
    localStorage.removeItem(WORKSPACE_STORAGE_KEY)
  } else {
    localStorage.setItem(WORKSPACE_STORAGE_KEY, workspaceId)
  }
}

const useWorkspace = () => {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [activeWorkspaceId, setActiveWorkspaceIdState] = useState<string | null>(
    getActiveWorkspaceId()
  )

  // Load active workspace ID from localStorage on mount
  useEffect(() => {
    const storedId = getActiveWorkspaceId()
    setActiveWorkspaceIdState(storedId)
  }, [])

  // Fetch all user workspaces
  const { data: workspacesData } = useQuery({
    queryKey: ["userWorkspaces"],
    queryFn: () => UserWorkspacesService.readWorkspaces(),
  })

  const workspaces = workspacesData?.data || []

  // Get the currently active workspace object
  const activeWorkspace: UserWorkspacePublic | "default" | null =
    activeWorkspaceId === null
      ? "default"
      : workspaces.find((w) => w.id === activeWorkspaceId) || null

  // Switch to a different workspace
  const switchWorkspace = (workspaceId: string | null) => {
    setActiveWorkspaceId(workspaceId)
    setActiveWorkspaceIdState(workspaceId)

    // Invalidate all entity queries to refetch with new workspace filter
    queryClient.invalidateQueries({ queryKey: ["agents"] })
    queryClient.invalidateQueries({ queryKey: ["credentials"] })
    queryClient.invalidateQueries({ queryKey: ["sessions"] })
    queryClient.invalidateQueries({ queryKey: ["activities"] })
  }

  // Create new workspace
  const createWorkspaceMutation = useMutation({
    mutationFn: (data: UserWorkspaceCreate) =>
      UserWorkspacesService.createWorkspace({ requestBody: data }),
    onSuccess: (newWorkspace) => {
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
    activeWorkspace,
    activeWorkspaceId,
    switchWorkspace,
    createWorkspaceMutation,
    deleteWorkspaceMutation,
    updateWorkspaceMutation,
  }
}

export { getActiveWorkspaceId, setActiveWorkspaceId }
export default useWorkspace
