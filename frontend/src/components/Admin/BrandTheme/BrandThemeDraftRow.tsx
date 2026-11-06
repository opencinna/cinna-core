import { useMutation, useQueryClient } from "@tanstack/react-query"
import { EyeOff, Loader2, Rocket, Trash2 } from "lucide-react"
import { useMemo, useState } from "react"

import { type BrandThemeDraftPublic, BrandThemeService } from "@/client"
import { parseBrandThemePreview } from "@/components/BrandTheme/brandTheme"
import { ListRow, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
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
import { getErrorMessage } from "@/utils"
import { BrandThemeSwatch } from "./BrandThemeSwatch"
import {
  invalidateBrandTheme,
  isPublished,
  useIsUnpublishing,
} from "./brandThemeQueries"
import { UnpublishBrandThemeDialog } from "./UnpublishBrandThemeDialog"

interface BrandThemeDraftRowProps {
  draft: BrandThemeDraftPublic
  /** Opens the preview dialog (S4) — where publishing happens. */
  onPreview: (draft: BrandThemeDraftPublic) => void
}

/**
 * One stored theme, in the Drafts card and in its Show-all Sheet. The row is
 * the Preview control; the `⋯` menu holds Publish (which opens the preview,
 * the publish's confirmation), Unpublish and Delete.
 */
export function BrandThemeDraftRow({
  draft,
  onPreview,
}: BrandThemeDraftRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirm, setConfirm] = useState<"unpublish" | "delete" | null>(null)
  const look = useMemo(
    () => parseBrandThemePreview(draft.document),
    [draft.document],
  )
  const published = isPublished(draft)
  const name = draft.name || "Untitled theme"
  const isUnpublishing = useIsUnpublishing()

  const deleteDraft = useMutation({
    mutationFn: () =>
      BrandThemeService.deleteBrandThemeDraft({ themeId: draft.id }),
    onSuccess: () => showSuccessToast(`Draft ${name} deleted`),
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "The draft was not deleted.")),
    onSettled: () => {
      setConfirm(null)
      return invalidateBrandTheme(queryClient)
    },
  })

  // What the spinner stands for, so the wait has a name.
  const pendingLabel = deleteDraft.isPending
    ? "Deleting…"
    : published && isUnpublishing
      ? "Unpublishing…"
      : null

  // Only a click in this row's own DOM opens the preview: the menu and the
  // confirms are portals whose React events still bubble up this tree. A
  // click on the `⋯` trigger is the menu's. And not while a write is in
  // flight — the spinner holds no button, so the click would land here.
  const openPreview = (e: React.SyntheticEvent) => {
    if (pendingLabel) return
    if (!e.currentTarget.contains(e.target as Node)) return
    if ((e.target as HTMLElement).closest("button")) return
    onPreview(draft)
  }

  const uploaded = formatRelativeTimestamp(draft.created_at)
  const lastPublished =
    !published && draft.revision != null && draft.published_at
      ? formatRelativeTimestamp(draft.published_at)
      : null

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the addon rows make.
    <div
      role="button"
      tabIndex={0}
      aria-label={`Preview the brand theme ${name}`}
      className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      onClick={openPreview}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return
        if (pendingLabel) return
        // Only the row itself: Enter on the `⋯` trigger is the menu's.
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        onPreview(draft)
      }}
    >
      <ListRow
        status={
          published
            ? { tone: "on", label: "Published — the server's theme" }
            : { tone: "off", label: "Draft" }
        }
        icon={<BrandThemeSwatch look={look} />}
        title={name}
        badges={
          draft.revision != null && (
            <Badge variant="secondary" className="h-5">
              rev {draft.revision}
            </Badge>
          )
        }
        // Several drafts of one theme share a name; the upload time is what
        // tells them apart.
        meta={uploaded && `Uploaded ${uploaded}`}
        flags={
          <RowInfo
            facts={[
              draft.created_by_email && `Uploaded by ${draft.created_by_email}`,
              lastPublished && `Last published ${lastPublished}`,
            ]}
          />
        }
      >
        {pendingLabel ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="flex h-7 w-7 items-center justify-center"
                aria-live="polite"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                <span className="sr-only">{pendingLabel}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              {pendingLabel}
            </TooltipContent>
          </Tooltip>
        ) : (
          <RowActionsMenu label={`the brand theme ${name}`}>
            {published ? (
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault()
                  setConfirm("unpublish")
                }}
              >
                <EyeOff />
                Unpublish
              </DropdownMenuItem>
            ) : (
              // Publishing happens in the preview, which is its
              // confirmation; the item only opens it. The menu closes as it
              // does, rather than staying open under the dialog.
              <DropdownMenuItem onSelect={() => onPreview(draft)}>
                <Rocket />
                Publish
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            {/* Disabled rather than hidden, with the reason as the label. */}
            <DropdownMenuItem
              variant={published ? undefined : "destructive"}
              disabled={published}
              onSelect={(e) => {
                e.preventDefault()
                setConfirm("delete")
              }}
            >
              <Trash2 />
              {published ? "Unpublish to delete" : "Delete draft"}
            </DropdownMenuItem>
          </RowActionsMenu>
        )}
      </ListRow>

      {/* The confirms are owned by the row, not nested in the menu items,
          which unmount on select and would take the pending state along. */}
      {confirm === "unpublish" && (
        <UnpublishBrandThemeDialog
          name={name}
          open
          onOpenChange={(open) => !open && setConfirm(null)}
        />
      )}

      <AlertDialog
        open={confirm === "delete"}
        onOpenChange={(open) => {
          if (!open && !deleteDraft.isPending) setConfirm(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Delete draft {name}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              Its files are removed unless another draft uses them. This can't
              be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteDraft.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                deleteDraft.mutate()
              }}
              disabled={deleteDraft.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteDraft.isPending ? "Deleting…" : "Delete draft"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
