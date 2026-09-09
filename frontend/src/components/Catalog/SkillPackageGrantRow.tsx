import { useMutation, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import { UserMinus } from "lucide-react"
import { useState } from "react"

import type { SkillPackageAccessGrantPublic } from "@/client"
import { SkillsService } from "@/client"
import { ListRow, RowInfo } from "@/components/Common/ListRow"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

interface SkillPackageGrantRowProps {
  packageId: string
  grant: SkillPackageAccessGrantPublic
}

/**
 * One person who can see this package, as a house list row.
 *
 * **No status dot:** membership *is* access, so a dot would say "yes" on every
 * row and nothing on none — the `BundlePermissionsCard` precedent.
 *
 * The connected-machines treatment (P5) for its single verb: one hover-revealed
 * destructive control and **no `⋯`**, because a menu holding one item costs a
 * click and hides the only thing the row does. `ListRow` already carries
 * `group`, so the control only needs the opacity rules.
 */
export function SkillPackageGrantRow({
  packageId,
  grant,
}: SkillPackageGrantRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const email = grant.user_email ?? "Unknown user"

  let grantedLabel: string | null = null
  try {
    grantedLabel = `Added ${formatDistanceToNow(new Date(grant.created_at), {
      addSuffix: true,
    })}`
  } catch {
    grantedLabel = null
  }

  const revokeMutation = useMutation({
    // Keyed on the **user**, not on the grant id — that is the route's shape.
    mutationFn: () =>
      SkillsService.revokeSkillPackageGrant({
        packageId,
        userId: grant.user_id,
      }),
    onSuccess: () => {
      showSuccessToast(`${email} no longer sees this skill`)
      setConfirmOpen(false)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to remove the person")),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: ["skills-catalog", "package", packageId, "grants"],
      }),
  })

  return (
    <ListRow title={email} flags={<RowInfo facts={[grantedLabel]} />}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
            aria-label={`Remove ${email}`}
            disabled={revokeMutation.isPending}
            onClick={() => setConfirmOpen(true)}
          >
            <UserMinus className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Remove
        </TooltipContent>
      </Tooltip>

      {/* Owned by the row, so it survives the trigger being hidden again on
          pointer-out and keeps its own pending state. */}
      <AlertDialog
        open={confirmOpen}
        onOpenChange={(next) => {
          if (!revokeMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {email}?</AlertDialogTitle>
            <AlertDialogDescription>
              They stop seeing this skill in the catalog. Agents they have
              already installed it into keep working.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                revokeMutation.mutate()
              }}
              disabled={revokeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {revokeMutation.isPending ? "Removing…" : "Remove"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
