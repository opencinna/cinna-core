import { Palette, TriangleAlert, Upload } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import type { BrandThemeDraftPublic, BrandThemeIssue } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AllBrandThemeDraftsSheet } from "./AllBrandThemeDraftsSheet"
import { BrandThemeDraftRow } from "./BrandThemeDraftRow"
import { BrandThemePreviewDialog } from "./BrandThemePreviewDialog"
import { isPublished, useBrandThemeDrafts } from "./brandThemeQueries"
import { draftsFull, draftsSummary, orderThemes } from "./draftsSummary"
import { UploadBrandThemeDialog } from "./UploadBrandThemeDialog"

const PURPOSE = "The look of the sign-in pages and Cinna Desktop"

interface PreviewTarget {
  id: string
  /** The upload's notes, when the preview follows an upload. */
  warnings: BrandThemeIssue[]
}

/**
 * Server Configuration → Interface: what this server's sign-in pages and
 * Cinna Desktop look like. Every stored theme package, the published one
 * pinned first; the one preview dialog every door opens (a row, its menu's
 * Publish, a successful upload) is mounted here.
 */
export function BrandThemeCard() {
  const drafts = useBrandThemeDrafts()
  const { data } = drafts
  const [uploadOpen, setUploadOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [preview, setPreview] = useState<PreviewTarget | null>(null)

  const themes = useMemo(() => orderThemes(data?.data ?? []), [data])
  const count = data?.count ?? 0
  const maxDrafts = data?.max_drafts
  const full = draftsFull(count, maxDrafts)
  // Only on loaded data and no failed refetch: an unknown state must not
  // claim "stock Cinna".
  const nothingLive =
    data !== undefined &&
    !drafts.isError &&
    themes.length > 0 &&
    !themes.some(isPublished)

  // Read from the list on every render, so the dialog shows the row as it is
  // now and closes by itself if the draft goes away.
  const previewDraft = preview
    ? themes.find((theme) => theme.id === preview.id)
    : undefined

  // A target the settled list does not hold (the refetch after an upload
  // failed, the draft was deleted) is dropped, so it can never pop open later
  // when some other refetch brings the row in.
  const stale = preview !== null && !previewDraft && !drafts.isFetching
  useEffect(() => {
    if (stale) setPreview(null)
  }, [stale])

  const openPreview = (theme: BrandThemeDraftPublic) =>
    setPreview({ id: theme.id, warnings: [] })

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <Palette className="h-5 w-5 shrink-0" />
            Brand theme
          </CardTitle>
          <Button
            size="sm"
            className="shrink-0"
            disabled={full || !data}
            onClick={() => setUploadOpen(true)}
          >
            <Upload className="h-4 w-4" />
            Upload
          </Button>
        </div>
        <CardDescription>
          {data ? `${PURPOSE} · ${draftsSummary(count, maxDrafts)}` : PURPOSE}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">
        {nothingLive && (
          <p className="text-sm text-muted-foreground">
            Nothing is published, so this server shows stock Cinna. Open a theme
            to preview and publish it.
          </p>
        )}
        {full && !drafts.isError && (
          <Alert className="[&>svg]:text-warning">
            <TriangleAlert />
            <AlertDescription>
              The draft limit is reached ({count} of {maxDrafts}). Delete a
              draft to upload another.
            </AlertDescription>
          </Alert>
        )}
        <PreviewList
          items={themes}
          total={count}
          getKey={(theme) => theme.id}
          renderItem={(theme) => (
            <BrandThemeDraftRow draft={theme} onPreview={openPreview} />
          )}
          isLoading={drafts.isLoading}
          isError={drafts.isError}
          error={drafts.error}
          onRetry={() => void drafts.refetch()}
          errorFallback="Couldn't load brand themes"
          empty={
            <p className="text-sm text-muted-foreground">
              No themes yet. Upload a theme package to preview and publish it;
              until then this server shows stock Cinna.
            </p>
          }
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <UploadBrandThemeDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onUploaded={(id, warnings) => setPreview({ id, warnings })}
      />
      <AllBrandThemeDraftsSheet
        themes={themes}
        count={count}
        maxDrafts={maxDrafts}
        onPreview={openPreview}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
      {previewDraft && preview && (
        <BrandThemePreviewDialog
          key={previewDraft.id}
          draft={previewDraft}
          warnings={preview.warnings}
          onClose={() => setPreview(null)}
        />
      )}
    </Card>
  )
}
