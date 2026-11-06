import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Moon, Sun } from "lucide-react"
import { useMemo, useState } from "react"

import {
  type BrandThemeDraftPublic,
  type BrandThemeIssue,
  BrandThemeService,
} from "@/client"
import { BrandThemePreview } from "@/components/BrandTheme/BrandThemePreview"
import {
  type BrandMode,
  parseBrandThemePreview,
} from "@/components/BrandTheme/brandTheme"
import { RelativeTime } from "@/components/Common/RelativeTime"
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
import { getErrorMessage } from "@/utils"
import { plural } from "@/utils/format"
import { BrandThemeIssueList } from "./BrandThemeIssueList"
import {
  invalidateBrandTheme,
  isPublished,
  publishRefusal,
} from "./brandThemeQueries"

/** Dark first, Light second — the order the issue list groups them in. */
const PANES: ReadonlyArray<readonly [BrandMode, string, typeof Moon]> = [
  ["dark", "Dark", Moon],
  ["light", "Light", Sun],
]

interface BrandThemePreviewDialogProps {
  draft: BrandThemeDraftPublic
  /**
   * The upload's notes, when this dialog follows an upload. The API does not
   * store them on the draft, so this is the one place they are shown.
   */
  warnings?: BrandThemeIssue[]
  onClose: () => void
}

/**
 * S4 — both modes of a theme as users would see them, and the one place a
 * draft is published from: a publish changes every sign-in page and every
 * Cinna Desktop on the server, so the preview is its confirmation.
 *
 * Renders from the list row it was opened with (the list carries `document`),
 * so it has no loading state of its own.
 */
export function BrandThemePreviewDialog({
  draft,
  warnings = [],
  onClose,
}: BrandThemePreviewDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const look = useMemo(
    () => parseBrandThemePreview(draft.document),
    [draft.document],
  )
  // Set by a 409: the draft no longer passes the (tightened) rules.
  const [refusal, setRefusal] = useState<BrandThemeIssue[] | null>(null)
  const published = isPublished(draft)
  const name = draft.name || "Untitled theme"

  const publish = useMutation({
    mutationFn: () =>
      BrandThemeService.publishBrandThemeDraft({ themeId: draft.id }),
    onSuccess: (live) => {
      showSuccessToast(
        `${live.name || name} published as revision ${live.revision}`,
      )
      onClose()
    },
    onError: (error) => {
      const issues = publishRefusal(error)
      if (issues) setRefusal(issues)
      else
        showErrorToast(getErrorMessage(error, "The theme was not published."))
    },
    onSettled: () => invalidateBrandTheme(queryClient),
  })

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !publish.isPending) onClose()
      }}
    >
      <DialogContent
        className="sm:max-w-5xl max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement | null)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="break-words">{name}</DialogTitle>
          <DialogDescription>
            {published ? (
              `Published · revision ${draft.revision}`
            ) : (
              <>
                Draft · uploaded <RelativeTime timestamp={draft.created_at} />
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        {refusal ? (
          <BrandThemeIssueList
            tone="error"
            issues={refusal}
            headline="This draft no longer passes validation — the rules have tightened since it was uploaded. Upload a fixed package."
          />
        ) : (
          warnings.length > 0 && (
            <BrandThemeIssueList
              tone="warning"
              issues={warnings}
              headline={`Uploaded with ${plural(warnings.length, "note")}`}
            />
          )
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {PANES.map(([mode, word, Icon]) => (
            <div key={mode} className="min-w-0 space-y-2">
              <p className="flex items-center gap-1.5 text-sm font-medium">
                <Icon className="h-4 w-4" />
                {word}
              </p>
              <BrandThemePreview theme={look} mode={mode} name={name} />
            </div>
          ))}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={publish.isPending}
          >
            Close
          </Button>
          {!published && (
            <LoadingButton
              type="button"
              loading={publish.isPending}
              // A refused draft is refused again until a fixed one is uploaded.
              disabled={refusal !== null}
              onClick={() => publish.mutate()}
            >
              {publish.isPending ? "Publishing…" : "Publish"}
            </LoadingButton>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
