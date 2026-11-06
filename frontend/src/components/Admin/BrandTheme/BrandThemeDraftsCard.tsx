import { Layers, TriangleAlert, Upload } from "lucide-react"
import { useState } from "react"

import type {
  BrandThemeDraftPublic,
  BrandThemeDraftsPublic,
  BrandThemeIssue,
} from "@/client"
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
import { draftsFull, draftsSummary } from "./draftsSummary"
import { UploadBrandThemeDialog } from "./UploadBrandThemeDialog"

interface BrandThemeDraftsCardProps {
  data: BrandThemeDraftsPublic | undefined
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  onPreview: (draft: BrandThemeDraftPublic) => void
  onUploaded: (id: string, warnings: BrandThemeIssue[]) => void
}

/**
 * S2 — every stored theme package, newest first, the published one marked by
 * its dot. Upload sits on the title row; Show all opens the Sheet.
 */
export function BrandThemeDraftsCard({
  data,
  isLoading,
  isError,
  error,
  onRetry,
  onPreview,
  onUploaded,
}: BrandThemeDraftsCardProps) {
  const [uploadOpen, setUploadOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const drafts = data?.data ?? []
  const count = data?.count ?? 0
  const maxDrafts = data?.max_drafts
  const full = draftsFull(count, maxDrafts)

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <Layers className="h-5 w-5 shrink-0" />
            Drafts
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
          {data
            ? draftsSummary(count, maxDrafts)
            : "Theme packages, newest first"}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">
        {full && (
          <Alert className="[&>svg]:text-warning">
            <TriangleAlert />
            <AlertDescription>
              The draft limit is reached ({count} of {maxDrafts}). Delete a
              draft to upload another.
            </AlertDescription>
          </Alert>
        )}
        <PreviewList
          items={drafts}
          total={count}
          getKey={(draft) => draft.id}
          renderItem={(draft) => (
            <BrandThemeDraftRow draft={draft} onPreview={onPreview} />
          )}
          isLoading={isLoading}
          isError={isError}
          error={error}
          onRetry={onRetry}
          errorFallback="Couldn't load drafts"
          empty={
            <p className="text-sm text-muted-foreground">
              No drafts yet. Upload a theme package to preview and publish it.
            </p>
          }
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <UploadBrandThemeDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onUploaded={onUploaded}
      />
      <AllBrandThemeDraftsSheet
        drafts={drafts}
        count={count}
        maxDrafts={maxDrafts}
        onPreview={onPreview}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
    </Card>
  )
}
