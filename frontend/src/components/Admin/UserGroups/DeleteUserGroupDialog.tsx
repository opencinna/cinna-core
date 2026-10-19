import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import { AdminUserGroupsService, type UserGroupPublic } from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { impactFacts, UserGroupImpactDialog } from "./UserGroupImpactDialog"
import {
  invalidateAllSharedResources,
  userGroupKeys,
} from "./userGroupQueries"

/** The key a group's delete runs under, so the list row can show it pending. */
export function deleteUserGroupMutationKey(groupId: string) {
  return ["delete-user-group", groupId] as const
}

interface DeleteUserGroupDialogProps {
  group: Pick<UserGroupPublic, "id" | "name">
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Opened from the group's own page: leave it for the list once deleted. */
  returnToList?: boolean
}

/** S4 — delete a group, after showing what its members lose. */
export function DeleteUserGroupDialog({
  group,
  open,
  onOpenChange,
  returnToList = false,
}: DeleteUserGroupDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const impact = useQuery({
    queryKey: userGroupKeys.deleteImpact(group.id),
    queryFn: () =>
      AdminUserGroupsService.getUserGroupDeleteImpact({ groupId: group.id }),
    enabled: open,
    // A preview of a destructive action is read fresh on every open.
    staleTime: 0,
  })

  const mutation = useMutation({
    mutationKey: deleteUserGroupMutationKey(group.id),
    mutationFn: () =>
      AdminUserGroupsService.deleteUserGroup({ groupId: group.id }),
    onSuccess: () => {
      showSuccessToast("Group deleted")
      onOpenChange(false)
      if (returnToList) void navigate({ to: "/admin/user-groups" })
      queryClient.removeQueries({ queryKey: userGroupKeys.detail(group.id) })
      invalidateAllSharedResources(queryClient)
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
    // Returned, so the row's pending state lasts until the refetch lands.
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: userGroupKeys.list }),
  })

  return (
    <UserGroupImpactDialog
      open={open}
      onOpenChange={onOpenChange}
      title={
        <>
          Delete group <em>{group.name}</em>?
        </>
      }
      impact={impact.data}
      impactError={impact.error}
      onRetryImpact={() => impact.refetch()}
      facts={
        impact.data
          ? impactFacts(impact.data, {
              members: "Members",
              shares: "Shared items",
              peopleLosing: "People losing credential access",
              links: true,
              aiEnvironments: true,
            })
          : []
      }
      noImpactSentence="Nobody loses access to anything."
      footnote="This can't be undone."
      confirmLabel="Delete group"
      pending={mutation.isPending}
      onConfirm={() => mutation.mutate()}
    />
  )
}
