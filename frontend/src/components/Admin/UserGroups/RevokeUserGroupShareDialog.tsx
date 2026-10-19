import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useRef } from "react"

import {
  AdminUserGroupsService,
  type UserGroupPublic,
  type UserGroupSharePublic,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  shareResourceName,
  USER_GROUP_SHARE_TYPES,
} from "@/utils/userGroupShareTypes"
import { impactFacts, UserGroupImpactDialog } from "./UserGroupImpactDialog"
import {
  invalidateRevokedResource,
  invalidateUserGroup,
  userGroupKeys,
} from "./userGroupQueries"

/** The key one share's revoke runs under, so its row can show it pending. */
export function revokeUserGroupShareMutationKey(
  groupId: string,
  share: Pick<UserGroupSharePublic, "resource_type" | "resource_id">,
) {
  return [
    "revoke-user-group-share",
    groupId,
    share.resource_type,
    share.resource_id,
  ] as const
}

/** What revoking costs, per type, in one sentence (S8). */
function revokeSentence(type: UserGroupSharePublic["resource_type"]): string {
  if (type === "credential") {
    return "Members with no other access lose it, and their agents stop using it."
  }
  if (type === "ai_credential") {
    return "Members' environments that use it are cleared, and running ones restart."
  }
  if (type === "agent_api_scopes") {
    return "Members lose these scopes from the next call unless they hold them another way."
  }
  return "Members lose access unless they have it another way."
}

interface RevokeUserGroupShareDialogProps {
  group: Pick<UserGroupPublic, "id" | "name">
  share: UserGroupSharePublic
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Focus target once the share is gone (its row's button goes too). */
  focusAfterRevoke?: () => void
}

/**
 * S8 — revoke one share from the group. The revoke-impact read gives the
 * numbers behind the per-type sentence, so this is the S4 fetched-impact
 * shell rather than a bare `AlertDialog`.
 */
export function RevokeUserGroupShareDialog({
  group,
  share,
  open,
  onOpenChange,
  focusAfterRevoke,
}: RevokeUserGroupShareDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const noun = USER_GROUP_SHARE_TYPES[share.resource_type].one
  const revoked = useRef(false)

  const impact = useQuery({
    queryKey: userGroupKeys.revokeImpact(
      group.id,
      share.resource_type,
      share.resource_id,
    ),
    queryFn: () =>
      AdminUserGroupsService.getUserGroupShareRevokeImpact({
        groupId: group.id,
        resourceType: share.resource_type,
        resourceId: share.resource_id,
      }),
    enabled: open,
    staleTime: 0,
  })

  const mutation = useMutation({
    mutationKey: revokeUserGroupShareMutationKey(group.id, share),
    mutationFn: () =>
      AdminUserGroupsService.revokeUserGroupShare({
        groupId: group.id,
        resourceType: share.resource_type,
        resourceId: share.resource_id,
      }),
    onSuccess: () => {
      revoked.current = true
      showSuccessToast(
        share.resource_type === "agent_api_scopes"
          ? "Scopes revoked"
          : "Access revoked",
      )
      onOpenChange(false)
      invalidateRevokedResource(
        queryClient,
        share.resource_type,
        share.resource_id,
      )
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
        share.resource_type === "agent_api_scopes" ? (
          <>
            Revoke <em>{group.name}</em>'s API scopes on{" "}
            <em>{shareResourceName(share)}</em>?
          </>
        ) : (
          <>
            Revoke <em>{group.name}</em>'s access to the {noun}{" "}
            <em>{shareResourceName(share)}</em>?
          </>
        )
      }
      description={revokeSentence(share.resource_type)}
      impact={impact.data}
      impactError={impact.error}
      onRetryImpact={() => impact.refetch()}
      facts={
        impact.data
          ? impactFacts(impact.data, {
              members: "Members",
              peopleLosing: "People losing access",
              links: true,
              aiEnvironments: true,
            })
          : []
      }
      noImpactSentence="The group has no members, so nobody loses access."
      confirmLabel={
        share.resource_type === "agent_api_scopes"
          ? "Revoke scopes"
          : "Revoke access"
      }
      pending={mutation.isPending}
      onConfirm={() => mutation.mutate()}
      onCloseAutoFocus={(event) => {
        if (revoked.current && focusAfterRevoke) {
          event.preventDefault()
          focusAfterRevoke()
        }
      }}
    />
  )
}
