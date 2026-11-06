import { useMutation, useQueryClient } from "@tanstack/react-query"
import { type FormEvent, useState } from "react"

import {
  type Body_brand_theme_upload_brand_theme_draft,
  type BrandThemeDraftsPublic,
  type BrandThemeIssue,
  BrandThemeService,
} from "@/client"
import {
  type UploadSource,
  UploadSourcePicker,
} from "@/components/Common/UploadSourcePicker"
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
  BRAND_THEME_DRAFTS_KEY,
  invalidateBrandTheme,
} from "./brandThemeQueries"

/**
 * A pick → the upload's form fields: a zip is the `package`; loose files are
 * `theme.json` as `theme` plus the rest as `files`. Null when loose files
 * carry no root `theme.json`.
 */
export function uploadFormData(
  source: UploadSource,
): Body_brand_theme_upload_brand_theme_draft | null {
  if (source.kind === "archive") return { package: source.file }
  const theme = source.files.find((file) => file.name === "theme.json")
  if (!theme) return null
  return { theme, files: source.files.filter((file) => file !== theme) }
}

interface UploadBrandThemeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** A draft was saved: the caller opens its preview with the notes. */
  onUploaded: (id: string, warnings: BrandThemeIssue[]) => void
}

/**
 * S3 — hand over a theme package and learn at once whether it is accepted.
 * A refusal keeps the dialog open with the pick and says exactly what to
 * fix; an accepted package closes it and opens the new draft's preview.
 */
export function UploadBrandThemeDialog({
  open,
  onOpenChange,
  onUploaded,
}: UploadBrandThemeDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [source, setSource] = useState<UploadSource | null>(null)
  const [refusal, setRefusal] = useState<BrandThemeIssue[] | null>(null)

  const close = () => {
    setSource(null)
    setRefusal(null)
    onOpenChange(false)
  }

  const upload = useMutation({
    mutationFn: (formData: Body_brand_theme_upload_brand_theme_draft) =>
      BrandThemeService.uploadBrandThemeDraft({ formData }),
    onSuccess: async (result) => {
      if (!result.id) {
        setRefusal(result.errors ?? [])
        return
      }
      // Held pending until the list has the new draft, so the preview that
      // follows has a row to render from and the toast can name it.
      await invalidateBrandTheme(queryClient)
      const drafts = queryClient.getQueryData<BrandThemeDraftsPublic>(
        BRAND_THEME_DRAFTS_KEY,
      )
      const name = drafts?.data.find((draft) => draft.id === result.id)?.name
      const warnings = result.warnings ?? []
      showSuccessToast(
        `${name ? `Draft ${name} uploaded` : "Draft uploaded"}${
          warnings.length > 0 ? ` with ${plural(warnings.length, "note")}` : ""
        }`,
      )
      close()
      onUploaded(result.id, warnings)
    },
    // 400, 413, 409 (draft limit): the server's words; the pick stays.
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "The upload failed.")),
  })

  const formData = source ? uploadFormData(source) : null
  const missingTheme = source !== null && formData === null

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (formData && !upload.isPending) upload.mutate(formData)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !upload.isPending) close()
      }}
    >
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>Upload theme package</DialogTitle>
          <DialogDescription>
            A zip of theme.json and assets/, or theme.json with its asset files.
            Nothing is saved unless the whole package passes.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <UploadSourcePicker
              source={source}
              onChange={(next) => {
                setSource(next)
                // The findings were about the previous pick.
                setRefusal(null)
              }}
              disabled={upload.isPending}
            />
            {missingTheme && (
              <p className="text-xs text-muted-foreground">
                theme.json is missing from the picked files.
              </p>
            )}
          </div>

          {refusal && (
            <BrandThemeIssueList
              tone="error"
              issues={refusal}
              headline={`Nothing was saved — fix ${plural(
                refusal.length,
                "problem",
              )} and upload again.`}
            />
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={close}
              disabled={upload.isPending}
            >
              Cancel
            </Button>
            <LoadingButton
              type="submit"
              loading={upload.isPending}
              disabled={!formData}
            >
              {upload.isPending ? "Uploading…" : "Upload"}
            </LoadingButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
