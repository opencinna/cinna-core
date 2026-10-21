import { useQuery } from "@tanstack/react-query"
import { useEffect, useRef } from "react"

import { type AgentPublic, type UserPublic, UsersService } from "@/client"
import { isLoggedIn } from "./useAuth"
import useCustomToast from "./useCustomToast"
import useWorkspace from "./useWorkspace"

/** What a detail page knows about its entity, once loaded. */
export interface EntityWorkspaceInfo {
  /** Identity of the entity; the decision is made once per id. */
  entityId: string
  ownerId: string | null | undefined
  /** `null` = Default workspace. */
  userWorkspaceId: string | null | undefined
  /** The entity shows in every workspace, so opening it never re-scopes. */
  workspaceAgnostic?: boolean
}

/**
 * Foreign-bundle installs carry no workspace and are listed in every
 * workspace (user_workspaces_tech.md, "Agent List Foreign-Install Handling").
 */
export const isWorkspaceAgnosticAgent = (
  agent: Pick<
    AgentPublic,
    "bundle_uuid" | "is_publisher_install" | "user_workspace_id"
  >,
): boolean =>
  agent.bundle_uuid != null &&
  !agent.is_publisher_install &&
  agent.user_workspace_id == null

/**
 * Switches THIS tab to the workspace an owned entity lives in, so the list
 * page the user clicks next shows it. Tab-only (never the global preference),
 * never navigates.
 *
 * Pass `undefined` until everything the decision needs is loaded (including
 * the agent when `workspaceAgnostic` depends on it). The decision is taken
 * once per `entityId`: refetches, re-renders, StrictMode's double effect and a
 * later manual switch on the same page never re-trigger it, so it cannot loop
 * or toast twice.
 */
export function useActivateEntityWorkspace(
  entity: EntityWorkspaceInfo | undefined,
) {
  const {
    workspacesEnabled,
    workspaces,
    workspacesLoaded,
    activeWorkspaceId,
    activateTabWorkspace,
  } = useWorkspace()
  const { showSuccessToast } = useCustomToast()
  const { data: currentUser } = useQuery<UserPublic | null, Error>({
    queryKey: ["currentUser"],
    queryFn: () => UsersService.readUserMe(),
    enabled: isLoggedIn(),
  })

  const decidedForRef = useRef<string | null>(null)

  const entityId = entity?.entityId
  const ownerId = entity?.ownerId
  const target = entity?.userWorkspaceId
  const agnostic = entity?.workspaceAgnostic ?? false

  // biome-ignore lint/correctness/useExhaustiveDependencies: decided once per entity; `activeWorkspaceId` / `workspaces` are read at decision time and must not re-trigger it
  useEffect(() => {
    if (!entityId || !currentUser || !workspacesLoaded) return
    if (decidedForRef.current === entityId) return
    decidedForRef.current = entityId

    if (!workspacesEnabled) return
    if (!ownerId || ownerId !== currentUser.id) return
    if (agnostic || target === undefined) return
    const targetWorkspace =
      target === null ? null : workspaces.find((w) => w.id === target)
    if (targetWorkspace === undefined) return
    if (target === activeWorkspaceId) return

    activateTabWorkspace(target)
    showSuccessToast(
      `Switched to "${targetWorkspace?.name || "Default"}" workspace for this tab`,
    )
  }, [entityId, ownerId, target, agnostic, currentUser, workspacesLoaded])
}

export default useActivateEntityWorkspace
