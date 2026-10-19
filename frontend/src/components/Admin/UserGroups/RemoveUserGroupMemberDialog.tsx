import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useRef } from "react"

import {
  AdminUserGroupsService,
  type UserGroupMemberPublic,
  type UserGroupPublic,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { impactFacts, UserGroupImpactDialog } from "./UserGroupImpactDialog"
import { invalidateUserGroup, userGroupKeys } from "./userGroupQueries"

/** The key a member's removal runs under, so the row can show it pending. */
export function removeUserGroupMemberMutationKey(
  groupId: string,
  userId: string,
) {
  return ["remove-user-group-member", groupId, userId] as const
}

export function memberDisplayName(member: UserGroupMemberPublic): string {
  return member.full_name || member.email
}

interface RemoveUserGroupMemberDialogProps {
  group: Pick<UserGroupPublic, "id" | "name">
  member: UserGroupMemberPublic
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Focus target once the member is gone (their row's button goes too). */
  focusAfterRemoval?: () => void
}

/** S7 — remove one member, after showing what they lose with it. */
export function RemoveUserGroupMemberDialog({
  group,
  member,
  open,
  onOpenChange,
  focusAfterRemoval,
}: RemoveUserGroupMemberDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const name = memberDisplayName(member)
  // Set before the close, so the focus handler that runs as the dialog
  // unmounts already knows the row is going away.
  const removed = useRef(false)

  const impact = useQuery({
    queryKey: userGroupKeys.removalImpact(group.id, member.user_id),
    queryFn: () =>
      AdminUserGroupsService.getUserGroupMemberRemovalImpact({
        groupId: group.id,
        userId: member.user_id,
      }),
    enabled: open,
    staleTime: 0,
  })

  const mutation = useMutation({
    mutationKey: removeUserGroupMemberMutationKey(group.id, member.user_id),
    mutationFn: () =>
      AdminUserGroupsService.removeUserGroupMember({
        groupId: group.id,
        userId: member.user_id,
      }),
    onSuccess: () => {
      removed.current = true
      showSuccessToast(`Removed ${name} from ${group.name}`)
      onOpenChange(false)
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
    // Returned, so the row's pending state lasts until the refetch lands.
    onSettled: () => invalidateUserGroup(queryClient, group.id),
  })

  return (
    <UserGroupImpactDialog
      open={open}
      onOpenChange={onOpenChange}
      title={
        <>
          Remove <em>{name}</em> from <em>{group.name}</em>?
        </>
      }
      description={`${name} stops reaching what is shared with this group, unless they can reach it another way.`}
      impact={impact.data}
      impactError={impact.error}
      onRetryImpact={() => impact.refetch()}
      facts={
        impact.data
          ? impactFacts(impact.data, {
              shares: "Shared items",
              links: true,
              aiEnvironments: true,
            })
          : []
      }
      // Every fact is 0 only when the group shares nothing: the server counts
      // the group's shares, not the ones this member reaches no other way.
      noImpactSentence={`Nothing is shared with this group, so ${name} loses no access.`}
      confirmLabel="Remove member"
      pending={mutation.isPending}
      onConfirm={() => mutation.mutate()}
      onCloseAutoFocus={(event) => {
        if (removed.current && focusAfterRemoval) {
          event.preventDefault()
          focusAfterRemoval()
        }
      }}
    />
  )
}
