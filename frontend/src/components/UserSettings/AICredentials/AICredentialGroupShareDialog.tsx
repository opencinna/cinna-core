import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import {
  type AICredentialGroupSharePublic,
  type AICredentialPublic,
  AiCredentialsService,
} from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  type GroupSelectedItem,
  UserAllowlistPicker,
} from "@/components/Common/UserAllowlistPicker"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
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
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { pluralize } from "@/utils/pluralize"

interface AICredentialGroupShareDialogProps {
  credential: AICredentialPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** "3 environments stop using this key; 1 running one restarts." */
function riskSentence(total: number, running: number): string {
  const head = `${pluralize(total, "environment stops", "environments stop")} using this key`
  if (running === 0) return `${head}.`
  return `${head}; ${running} running ${running === 1 ? "one restarts" : "ones restart"}.`
}

/**
 * "Share with groups" for one AI credential the user owns (D9: group-only in
 * v1, never an admin-managed key). Auto-save: every add and remove is one
 * write and a toast, so the footer is a single Done. A removal that clears
 * members' environments confirms in an `AlertDialog` (the one dialog allowed
 * on top of a dialog), with the numbers already on the list row.
 */
export function AICredentialGroupShareDialog({
  credential,
  open,
  onOpenChange,
}: AICredentialGroupShareDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [pendingRemove, setPendingRemove] =
    useState<AICredentialGroupSharePublic | null>(null)

  const sharesKey = ["ai-credential-group-shares", credential.id]
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: sharesKey,
    queryFn: () =>
      AiCredentialsService.listAiCredentialGroupShares({
        credentialId: credential.id,
      }),
    enabled: open,
  })
  const shares = data?.data ?? []

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: sharesKey })
    // The row's "Shared with N groups" flag reads `group_share_count`.
    queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
  }

  const addMutation = useMutation({
    mutationFn: (group: { id: string; name: string }) =>
      AiCredentialsService.shareAiCredentialWithGroup({
        credentialId: credential.id,
        requestBody: { group_id: group.id },
      }),
    onSuccess: (_, group) => {
      showSuccessToast(`Shared with ${group.name}`)
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })

  const removeMutation = useMutation({
    mutationFn: (group: { id: string; name: string }) =>
      AiCredentialsService.revokeAiCredentialGroupShare({
        credentialId: credential.id,
        groupId: group.id,
      }),
    onSuccess: (_, group) => {
      showSuccessToast(`Stopped sharing with ${group.name}`)
      setPendingRemove(null)
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })

  const selected: GroupSelectedItem[] = shares.map((s) => ({
    id: s.group_id,
    groupId: s.group_id,
    name: s.group_name,
    memberCount: s.member_count,
  }))

  const remove = (item: GroupSelectedItem) => {
    const share = shares.find((s) => s.group_id === item.groupId)
    if (share && (share.environments_at_risk?.total ?? 0) > 0) {
      setPendingRemove(share)
    } else {
      removeMutation.mutate({ id: item.groupId, name: item.name })
    }
  }

  const pendingTotal = pendingRemove?.environments_at_risk?.total ?? 0
  const pendingRunning = pendingRemove?.environments_at_risk?.running ?? 0

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0"
          // The first focusable control can be a pill's remove button, whose
          // tooltip would open over nothing on mount; the body takes focus.
          onOpenAutoFocus={(e) => {
            e.preventDefault()
            ;(e.currentTarget as HTMLElement | null)?.focus()
          }}
        >
          <DialogHeader>
            <DialogTitle className="break-words">
              Share {credential.name}
            </DialogTitle>
            <DialogDescription>
              Members can choose this key for their environments. They never see
              its value.
            </DialogDescription>
          </DialogHeader>

          {isError ? (
            <QueryErrorAlert
              error={error}
              fallback="Couldn't load the groups it is shared with"
              onRetry={() => refetch()}
              compact
            />
          ) : isLoading ? (
            <div className="space-y-2">
              <div className="flex flex-wrap gap-1.5">
                <Skeleton className="h-6 w-28 rounded-full" />
                <Skeleton className="h-6 w-20 rounded-full" />
              </div>
              <Skeleton className="h-9 w-full" />
            </div>
          ) : (
            <UserAllowlistPicker
              people={false}
              label={null}
              enabled={open}
              groups={{
                selected,
                onAdd: (g) => addMutation.mutate(g),
                onRemove: remove,
                isAdding: addMutation.isPending,
                isRemoving: removeMutation.isPending,
              }}
              searchPlaceholder="Search groups..."
              emptyHint="Not shared with any group yet."
            />
          )}

          <DialogFooter>
            <Button type="button" onClick={() => onOpenChange(false)}>
              Done
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={pendingRemove !== null}
        onOpenChange={(next) => {
          // Escape must not close the confirm mid-request.
          if (!next && !removeMutation.isPending) setPendingRemove(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Stop sharing with {pendingRemove?.group_name}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {riskSentence(pendingTotal, pendingRunning)}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <LoadingButton
              variant="destructive"
              loading={removeMutation.isPending}
              onClick={() =>
                pendingRemove &&
                removeMutation.mutate({
                  id: pendingRemove.group_id,
                  name: pendingRemove.group_name,
                })
              }
            >
              Stop sharing
            </LoadingButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
