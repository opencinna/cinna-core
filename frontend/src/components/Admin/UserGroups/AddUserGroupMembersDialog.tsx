import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { AdminUserGroupsService, type UserGroupPublic } from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { pluralize } from "@/utils/pluralize"
import { invalidateUserGroup } from "./userGroupQueries"

interface AddUserGroupMembersDialogProps {
  group: Pick<UserGroupPublic, "id" | "name">
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Members on the loaded page, kept out of the search results. */
  memberUserIds: string[]
}

/**
 * S6 — pick people, then add them in one request. The picker buffers: its
 * pills are the people chosen so far, and nothing is written until "Add N
 * members".
 */
export function AddUserGroupMembersDialog({
  group,
  open,
  onOpenChange,
  memberUserIds,
}: AddUserGroupMembersDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [chosen, setChosen] = useState<UserAllowlistSelectedItem[]>([])

  const mutation = useMutation({
    mutationFn: (userIds: string[]) =>
      AdminUserGroupsService.addUserGroupMembers({
        groupId: group.id,
        requestBody: { user_ids: userIds },
      }),
    onSuccess: (added, userIds) => {
      // The server is idempotent and returns only the rows it added.
      const addedCount = added.data.length
      const already = userIds.length - addedCount
      showSuccessToast(
        already > 0
          ? `Added ${pluralize(addedCount, "member", "members")} · ${already} ${already === 1 ? "was" : "were"} already in the group`
          : `Added ${pluralize(addedCount, "member", "members")}`,
      )
      setChosen([])
      onOpenChange(false)
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
    onSettled: () => invalidateUserGroup(queryClient, group.id),
  })

  const close = (next: boolean) => {
    // Escape and outside-click must not close a dialog mid-request.
    if (mutation.isPending) return
    if (!next) setChosen([])
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            Add members to <em>{group.name}</em>
          </DialogTitle>
          <DialogDescription>
            They get everything shared with the group from the moment they are
            added.
          </DialogDescription>
        </DialogHeader>

        <UserAllowlistPicker
          selected={chosen}
          onAdd={(user) =>
            setChosen((current) => [
              ...current,
              {
                id: user.id,
                userId: user.id,
                fallbackLabel: user.full_name || user.email,
              },
            ])
          }
          onRemove={(item) =>
            setChosen((current) => current.filter((c) => c.id !== item.id))
          }
          isAdding={mutation.isPending}
          isRemoving={mutation.isPending}
          searchPlaceholder="Search people by name or email"
          label={null}
          enabled={open}
          excludeUserIds={memberUserIds}
          includeSelf
        />

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={mutation.isPending}
            onClick={() => close(false)}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={mutation.isPending}
            disabled={chosen.length === 0}
            onClick={() => mutation.mutate(chosen.map((c) => c.userId))}
          >
            {chosen.length === 0
              ? "Add members"
              : `Add ${pluralize(chosen.length, "member", "members")}`}
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
