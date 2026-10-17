import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Download, FileX, Loader2, Trash2 } from "lucide-react"
import type { KeyboardEvent, SyntheticEvent } from "react"
import { useState } from "react"

import {
  ApiError,
  type ArtifactRevisionPublic,
  ArtifactsService,
} from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { getErrorMessage } from "@/utils"
import { ArtifactConfirmDialog } from "../ArtifactConfirmDialog"
import { formatBytes, plural } from "../artifactFormat"
import { artifactKeys, invalidateArtifact } from "../artifactQueries"
import { ExcludedFilesDialog } from "./ExcludedFilesDialog"

interface RevisionRowProps {
  artifactId: string
  artifactTitle: string
  revision: ArtifactRevisionPublic
  /** This row is the revision on screen. */
  isViewed: boolean
  onView: (number: number) => void
}

/**
 * One revision (owner only), rendered by the details panel and its Show-all
 * Sheet. The row is the control that views it; the `⋯` holds download, the
 * left-out files and delete. It owns its confirm so the pending state stays
 * on the row that is writing.
 */
export function RevisionRow({
  artifactId,
  artifactTitle,
  revision,
  isViewed,
  onView,
}: RevisionRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [excludedOpen, setExcludedOpen] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const excluded = revision.excluded ?? []
  const canDelete = !revision.is_current && !revision.pinned

  const remove = useMutation({
    mutationFn: () =>
      ArtifactsService.deleteArtifactRevision({
        artifactId,
        number: revision.number,
      }),
    onSuccess: () => {
      showSuccessToast(`Revision ${revision.number} was deleted`)
      setConfirmOpen(false)
    },
    onError: (err) =>
      setRefusal(getErrorMessage(err, "Couldn't delete the revision")),
    onSettled: (_, err) => {
      if (err instanceof ApiError && err.status === 409) return
      return invalidateArtifact(queryClient, artifactId)
    },
  })

  // A zip of this revision through a capability (D18): mint, then follow the
  // cap's zip URL — no bearer token in a blob.
  const download = async () => {
    try {
      const minted = await queryClient.fetchQuery({
        queryKey: artifactKeys.view(artifactId, revision.number),
        queryFn: () =>
          ArtifactsService.viewArtifact({ artifactId, rev: revision.number }),
        staleTime: 60_000,
      })
      window.location.assign(minted.download_url)
    } catch (err) {
      showErrorToast(getErrorMessage(err, "Couldn't download the revision"))
    }
  }

  // The row is the "view this revision" control. Guards: portaled menus and
  // dialogs bubble React events up this tree (`contains`); the `⋯` trigger is
  // the menu's (`closest("button")`); and a row being deleted is not a way in.
  const view = (e: SyntheticEvent) => {
    if (remove.isPending) return
    if (!e.currentTarget.contains(e.target as Node)) return
    if ((e.target as HTMLElement).closest("button")) return
    onView(revision.number)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" && e.key !== " ") return
    if (remove.isPending || e.target !== e.currentTarget) return
    e.preventDefault()
    onView(revision.number)
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the addon rows make.
    <div
      role="button"
      tabIndex={0}
      aria-current={isViewed ? "true" : undefined}
      aria-label={`View revision ${revision.number}${revision.is_current ? ", current" : ""}`}
      className={cn(
        "cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        isViewed && "bg-muted",
      )}
      onClick={view}
      onKeyDown={onKeyDown}
    >
      <ListRow
        title={`Revision ${revision.number}`}
        badges={
          revision.is_current ? (
            <Badge variant="secondary" className="h-5 shrink-0">
              Current
            </Badge>
          ) : undefined
        }
        meta={
          <>
            <RelativeTime timestamp={revision.created_at} /> ·{" "}
            {formatBytes(revision.size_bytes)}
          </>
        }
        flags={
          <>
            {excluded.length > 0 && (
              <RowFlag
                icon={AlertTriangle}
                label={`${plural(excluded.length, "file")} ${excluded.length === 1 ? "was" : "were"} left out`}
              />
            )}
            <RowInfo
              facts={[
                plural(revision.file_count, "file"),
                revision.note || null,
                revision.created_by_name
                  ? `Published by ${revision.created_by_name}`
                  : null,
              ]}
            />
          </>
        }
      >
        {remove.isPending ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="flex h-7 w-7 items-center justify-center"
                aria-live="polite"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                <span className="sr-only">
                  Deleting revision {revision.number}…
                </span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Deleting…
            </TooltipContent>
          </Tooltip>
        ) : (
          <RowActionsMenu label={`revision ${revision.number}`}>
            <DropdownMenuItem onSelect={() => void download()}>
              <Download />
              Download revision
            </DropdownMenuItem>
            {excluded.length > 0 && (
              <DropdownMenuItem onSelect={() => setExcludedOpen(true)}>
                <FileX />
                Files left out…
              </DropdownMenuItem>
            )}
            {canDelete && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() => {
                    setRefusal(null)
                    setConfirmOpen(true)
                  }}
                >
                  <Trash2 />
                  Delete revision
                </DropdownMenuItem>
              </>
            )}
          </RowActionsMenu>
        )}
      </ListRow>

      <ExcludedFilesDialog
        open={excludedOpen}
        onOpenChange={setExcludedOpen}
        revisionNumber={revision.number}
        artifactTitle={artifactTitle}
        paths={excluded}
      />
      <ArtifactConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={
          <>
            Delete revision {revision.number} of <em>{artifactTitle}</em>?
          </>
        }
        description="Its files are removed. This can't be undone."
        actionLabel="Delete revision"
        pendingLabel="Deleting…"
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
        error={refusal}
      />
    </div>
  )
}
