import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Check, Copy, FileText, Package, Trash2 } from "lucide-react"
import { useState } from "react"

import type { AgentBundleRevisionPublic } from "@/client"
import { BundlesService } from "@/client"
import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

/** "v1.3" when the publisher named a version, "rev 4" when they did not. */
export function revisionLabel(rev: AgentBundleRevisionPublic) {
  return rev.version ? `v${rev.version}` : `rev ${rev.revision_number}`
}

interface BundleRevisionRowProps {
  agentId: string
  bundleUuid: string
  rev: AgentBundleRevisionPublic
  /** This revision is the bundle's `latest_revision_id`. */
  isCurrent: boolean
  /** The publisher's own install is sitting on this revision. */
  isInstalled: boolean
}

/**
 * One published revision, as a house list row.
 *
 * Rendered by both the Revisions card and its "Show all" Sheet, so the two
 * cannot drift, and it owns its copy state and its delete confirm for the same
 * reason `ScheduleRow` does: pending state stays on the row that is writing,
 * and the confirm opens over the Sheet without closing it.
 */
export function BundleRevisionRow({
  agentId,
  bundleUuid,
  rev,
  isCurrent,
  isInstalled,
}: BundleRevisionRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  // The API refuses a revision any install still references, so the row says
  // so before the click rather than after the 409.
  const installCount = rev.install_count ?? 0
  const canDelete = installCount <= 0
  const label = revisionLabel(rev)

  const deleteMutation = useMutation({
    mutationFn: () =>
      BundlesService.deleteRevision({ bundleUuid, revisionId: rev.id }),
    onSuccess: () => {
      showSuccessToast(`Revision ${label} deleted`)
      setConfirmOpen(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      queryClient.invalidateQueries({ queryKey: ["bundles", bundleUuid] })
      queryClient.invalidateQueries({
        queryKey: ["bundles", bundleUuid, "revisions"],
      })
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to delete revision")),
  })

  const handleCopyHash = async () => {
    try {
      await navigator.clipboard.writeText(rev.content_hash)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  return (
    <ListRow
      // The history is append-only, so "current" is the one state a revision
      // has — and it is the leading dot rather than a "current" badge, which
      // the eye had to read on every row to find the one row it was not on.
      status={{
        tone: isCurrent ? "on" : "off",
        label: isCurrent ? "Current revision" : "Superseded revision",
      }}
      title={
        <>
          {label}
          {rev.version && (
            <span className="ml-1.5 text-xs font-normal text-muted-foreground">
              rev {rev.revision_number}
            </span>
          )}
        </>
      }
      meta={`${installCount} install${
        installCount === 1 ? "" : "s"
      } · ${new Date(rev.published_at).toLocaleDateString()}`}
      flags={
        <>
          {isInstalled && !isCurrent && (
            <RowFlag icon={Package} label="This install is on this revision" />
          )}
          {/* Release notes are prose of unknown length: as a third line they
              would set the height of every row in the list off the longest
              one. */}
          {rev.release_notes && (
            <RowFlag icon={FileText} label={rev.release_notes} />
          )}
        </>
      }
    >
      <RowActionsMenu
        label={`the revision ${label}`}
        disabled={deleteMutation.isPending}
      >
        <DropdownMenuItem
          onSelect={(e) => {
            // The item unmounts on select and would take the "Copied" tick
            // with it.
            e.preventDefault()
            handleCopyHash()
          }}
        >
          {copied ? <Check /> : <Copy />}
          {copied ? "Copied" : "Copy content hash"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {/* Disabled rather than hidden, with the reason as the label: "why
            can't I delete this one" is the whole question a publisher has
            here, and a missing item does not answer it. */}
        <DropdownMenuItem
          variant={canDelete ? "destructive" : undefined}
          disabled={!canDelete}
          onSelect={(e) => {
            e.preventDefault()
            setConfirmOpen(true)
          }}
        >
          <Trash2 />
          {canDelete
            ? "Delete revision"
            : `In use by ${installCount} install${
                installCount === 1 ? "" : "s"
              }`}
        </DropdownMenuItem>
      </RowActionsMenu>

      <AlertDialog
        open={confirmOpen}
        onOpenChange={(next) => {
          if (!deleteMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {label}?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the revision row, its on-disk snapshot,
              and rewires the bundle's "current" pointer to the previous
              revision if needed. The publisher install stays — it just won't be
              on this revision anymore. The API rejects deletion if any other
              user's install still references this revision.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                deleteMutation.mutate()
              }}
              disabled={deleteMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete revision"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
