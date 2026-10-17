import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { AlertCircle, Upload } from "lucide-react"
import { type FormEvent, type ReactNode, useState } from "react"

import { type ArtifactPublished, ArtifactsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { getErrorMessage } from "@/utils"
import {
  ArtifactDetailsFields,
  type ArtifactDetailsValues,
} from "./ArtifactDetailsFields"
import { formatBytes, plural } from "./artifactFormat"
import { invalidateArtifact, useArtifactCapabilities } from "./artifactQueries"
import { PublishResultPanel } from "./PublishResultPanel"
import { type UploadSource, UploadSourcePicker } from "./UploadSourcePicker"

const EMPTY: ArtifactDetailsValues = {
  title: "",
  description: "",
  startPage: "",
}

/** The client-side pre-check against the server's limits, as a sentence. */
function limitProblem(
  source: UploadSource | null,
  limits:
    | { max_revision_bytes: number; max_revision_files: number }
    | undefined,
): string | null {
  if (!source || !limits) return null
  if (source.size > limits.max_revision_bytes) {
    return `${formatBytes(source.size)} is over the ${formatBytes(limits.max_revision_bytes)} limit.`
  }
  if (
    source.kind === "files" &&
    source.files.length > limits.max_revision_files
  ) {
    return `${plural(source.files.length, "file")} is over the ${limits.max_revision_files.toLocaleString()}-file limit.`
  }
  return null
}

/**
 * S3: put a zip, a folder or files into Cinna. The trigger defaults to the
 * page header's primary Upload button; the empty list passes its own.
 */
export function UploadArtifactDialog({ trigger }: { trigger?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [source, setSource] = useState<UploadSource | null>(null)
  const [values, setValues] = useState<ArtifactDetailsValues>(EMPTY)
  const [result, setResult] = useState<ArtifactPublished | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast } = useCustomToast()
  const { activeWorkspaceId } = useWorkspace()
  const capabilities = useArtifactCapabilities()

  const mutation = useMutation({
    mutationFn: (picked: UploadSource) =>
      ArtifactsService.createArtifact({
        formData: {
          title: values.title.trim(),
          description: values.description.trim() || null,
          entry_path: values.startPage.trim() || null,
          source_kind: "upload",
          user_workspace_id: activeWorkspaceId || null,
          archive: picked.kind === "archive" ? picked.file : null,
          files: picked.kind === "files" ? picked.files : null,
        },
      }),
    onSuccess: (published) => {
      void invalidateArtifact(queryClient)
      if ((published.excluded ?? []).length === 0) {
        showSuccessToast(
          `Artifact uploaded — ${plural(published.file_count ?? 0, "file")}, ${formatBytes(published.size_bytes)}`,
        )
        close()
        navigate({
          to: "/artifact/$artifactId",
          params: { artifactId: published.id },
        })
      } else {
        setResult(published)
      }
    },
  })

  function close() {
    setOpen(false)
    setSource(null)
    setValues(EMPTY)
    setResult(null)
    mutation.reset()
  }

  const pickSource = (next: UploadSource | null) => {
    setSource(next)
    mutation.reset()
    // Prefill the title from the pick, unless the user already typed one.
    if (next && !values.title.trim()) {
      setValues((v) => ({ ...v, title: next.suggestedTitle }))
    }
  }

  const problem = limitProblem(source, capabilities.data?.limits)
  const canSubmit =
    source !== null && values.title.trim() !== "" && problem === null

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (source && canSubmit && !mutation.isPending) mutation.mutate(source)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) setOpen(true)
        else if (!mutation.isPending) close()
      }}
    >
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm">
            <Upload className="h-4 w-4" />
            Upload
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>Upload artifact</DialogTitle>
          <DialogDescription>
            A zip, a folder or files. An index.html at the top opens as a site.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <PublishResultPanel result={result} onClose={close} />
        ) : (
          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-1.5">
              <UploadSourcePicker
                source={source}
                onChange={pickSource}
                disabled={mutation.isPending}
              />
              {problem && (
                <p className="text-sm text-destructive" role="alert">
                  {problem}
                </p>
              )}
            </div>

            <ArtifactDetailsFields
              idPrefix="artifact-upload"
              values={values}
              onChange={setValues}
              disabled={mutation.isPending}
              startPage={{ mode: "text" }}
            />

            {mutation.isError && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertDescription className="break-words">
                  {getErrorMessage(mutation.error, "The upload failed.")}
                </AlertDescription>
              </Alert>
            )}

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={close}
                disabled={mutation.isPending}
              >
                Cancel
              </Button>
              <LoadingButton
                type="submit"
                loading={mutation.isPending}
                disabled={!canSubmit}
              >
                {mutation.isPending ? "Uploading…" : "Upload"}
              </LoadingButton>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
