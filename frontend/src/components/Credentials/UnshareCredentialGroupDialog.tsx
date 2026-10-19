/**
 * Confirm for unsharing a credential from a user group, opened only when
 * members' agents link it (`links_at_risk > 0`, read off the share list); a
 * plain group unshare stays one click. Mirrors `UnshareCredentialDialog`: a
 * `Dialog` because it carries fetched impact (§2 Confirmation).
 */
import type { CredentialGroupSharePublic } from "@/client"
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
import { pluralize } from "@/utils/pluralize"

interface UnshareCredentialGroupDialogProps {
  credentialName: string
  /** The group share being removed; `null` closes the dialog. */
  share: CredentialGroupSharePublic | null
  isPending: boolean
  onConfirm: (share: CredentialGroupSharePublic) => void
  onClose: () => void
}

export function UnshareCredentialGroupDialog({
  credentialName,
  share,
  isPending,
  onConfirm,
  onClose,
}: UnshareCredentialGroupDialogProps) {
  const links = share?.links_at_risk ?? 0

  return (
    <Dialog
      open={share !== null}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!next && !isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            Stop sharing with {share?.group_name}?
          </DialogTitle>
          <DialogDescription className="break-words">
            Members with no other access to {credentialName} lose it right away,
            and their agents stop using it.
          </DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Agent links removed</dt>
          <dd className="font-medium">{pluralize(links, "link", "links")}</dd>
        </dl>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <LoadingButton
            variant="destructive"
            loading={isPending}
            onClick={() => share && onConfirm(share)}
          >
            Stop sharing
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
