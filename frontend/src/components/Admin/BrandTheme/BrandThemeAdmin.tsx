import { useEffect, useState } from "react"

import type { BrandThemeDraftPublic, BrandThemeIssue } from "@/client"
import { BrandThemeDraftsCard } from "./BrandThemeDraftsCard"
import { BrandThemePreviewDialog } from "./BrandThemePreviewDialog"
import { isPublished, useBrandThemeDrafts } from "./brandThemeQueries"
import { PublishedThemeCard } from "./PublishedThemeCard"

interface PreviewTarget {
  id: string
  /** The upload's notes, when the preview follows an upload. */
  warnings: BrandThemeIssue[]
}

/**
 * The body of `/admin/brand-theme`: the Published card and the Drafts card
 * from one query, and the one preview dialog every door opens (a row, the
 * Published card's Preview, a successful upload).
 */
export function BrandThemeAdmin() {
  const drafts = useBrandThemeDrafts()
  const [preview, setPreview] = useState<PreviewTarget | null>(null)

  const rows = drafts.data?.data ?? []
  const published = rows.find(isPublished) ?? null
  // Read from the list on every render, so the dialog shows the row as it is
  // now and closes by itself if the draft goes away.
  const previewDraft = preview
    ? rows.find((draft) => draft.id === preview.id)
    : undefined

  // A target the settled list does not hold (the refetch after an upload
  // failed, the draft was deleted) is dropped, so it can never pop open later
  // when some other refetch brings the row in.
  const stale = preview !== null && !previewDraft && !drafts.isFetching
  useEffect(() => {
    if (stale) setPreview(null)
  }, [stale])

  const openPreview = (draft: BrandThemeDraftPublic) =>
    setPreview({ id: draft.id, warnings: [] })
  const query = {
    isLoading: drafts.isLoading,
    isError: drafts.isError,
    error: drafts.error,
    onRetry: () => void drafts.refetch(),
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 items-start">
      <PublishedThemeCard
        published={published}
        onPreview={openPreview}
        {...query}
      />
      <BrandThemeDraftsCard
        data={drafts.data}
        onPreview={openPreview}
        onUploaded={(id, warnings) => setPreview({ id, warnings })}
        {...query}
      />
      {previewDraft && preview && (
        <BrandThemePreviewDialog
          key={previewDraft.id}
          draft={previewDraft}
          warnings={preview.warnings}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )
}
