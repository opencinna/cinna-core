/**
 * One person who got somebody else's connection by installing a bundle or
 * skill the viewer published (S4b), in the connected-machines shape: the row's
 * only mutation is "cut this one off", hover-revealed, with no `⋯`.
 *
 * Rendered by the card and its "Show all" Sheet so the two cannot drift. The
 * confirm is owned by the row; the mutation is the card's, so the pending
 * state is scoped to the row by share id.
 */
import { Box, GraduationCap, Loader2, UserMinus } from "lucide-react"
import { useState } from "react"

import type { CredentialSharePublic } from "@/client"
import { ListRow, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
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
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { UsageTile } from "./CredentialUsageRows"

interface CredentialReshareRowProps {
  share: CredentialSharePublic
  credentialName: string
  ownerEmail: string
  isPending: boolean
  /** Revoke this share; `onDone` closes the confirm on success. */
  onRemove: (shareId: string, onDone: () => void) => void
}

export function CredentialReshareRow({
  share,
  credentialName,
  ownerEmail,
  isPending,
  onRemove,
}: CredentialReshareRowProps) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const via = share.reshared_via
  const kind = via?.package_kind === "skill" ? "skill" : "bundle"
  const packageName = via?.package_name || ""
  const sharedAt = formatRelativeTimestamp(share.shared_at)
  const person = share.shared_with_email

  return (
    <ListRow
      icon={<UsageTile icon={kind === "skill" ? GraduationCap : Box} />}
      title={person}
      meta={packageName || undefined}
      flags={<RowInfo facts={[sharedAt && `Shared ${sharedAt}`]} />}
    >
      {isPending ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex h-7 w-7 items-center justify-center">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              <span className="sr-only">Removing…</span>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Removing…
          </TooltipContent>
        </Tooltip>
      ) : (
        // Hover-revealed (P3 variant): a View list whose one control is
        // destructive.
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
              aria-label={`Remove ${person}'s access`}
              onClick={() => setConfirmOpen(true)}
            >
              <UserMinus className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Remove access
          </TooltipContent>
        </Tooltip>
      )}

      <AlertDialog
        open={confirmOpen}
        onOpenChange={(next) => {
          // Escape and outside-click must not close a dialog mid-request.
          if (!isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Remove {person}'s access to {credentialName}?
            </AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              Their agents lose this connection now, and updates of your {kind}
              {packageName ? ` ${packageName}` : ""} will not give it back to
              them. {ownerEmail || "The owner"} can still share it with them
              directly.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isPending}>Cancel</AlertDialogCancel>
            <LoadingButton
              variant="destructive"
              loading={isPending}
              onClick={() => onRemove(share.id, () => setConfirmOpen(false))}
            >
              Remove access
            </LoadingButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
