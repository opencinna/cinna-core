import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { AlertCircle } from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"
import { toast } from "sonner"

import { ApiError, type ArtifactPublished, ArtifactsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { Skeleton } from "@/components/ui/skeleton"
import { getErrorMessage } from "@/utils"
import {
  ArtifactDetailsFields,
  type ArtifactDetailsValues,
} from "./ArtifactDetailsFields"
import { artifactTitle } from "./artifactFormat"
import { invalidateArtifact } from "./artifactQueries"
import { PublishResultPanel } from "./PublishResultPanel"

export interface PublishTarget {
  /** Workspace-relative path, as the tree names it ("files/report"). */
  path: string
  isFolder: boolean
}

interface PublishFromWorkspaceDialogProps {
  environmentId: string
  /** Earlier publishes are matched per agent: `source_key = <agent_id>:<path>`. */
  agentId: string
  target: PublishTarget | null
  onClose: () => void
}

function stripExtension(name: string): string {
  const dot = name.lastIndexOf(".")
  return dot > 0 ? name.slice(0, dot) : name
}

/** The sentence for a refused publish, by status (S10 States). */
function publishError(err: unknown): string {
  const status = err instanceof ApiError ? err.status : null
  if (status === 409) {
    const message = getErrorMessage(err, "")
    // 409 also means "this path was published from another source".
    return message.includes("another source")
      ? message
      : "Start the environment, then publish again."
  }
  if (status === 403)
    return "Only the agent's owner can publish from its workspace."
  if (status === 404)
    return "This file or folder is no longer in the workspace."
  return getErrorMessage(err, "Couldn't publish it.")
}

/**
 * S10 — turn a file or folder an agent built into an artifact. When the path
 * was published before, this updates that artifact (the server matches the
 * path, so the dialog says so rather than offering a choice it cannot keep).
 */
export function PublishFromWorkspaceDialog({
  environmentId,
  agentId,
  target,
  onClose,
}: PublishFromWorkspaceDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [values, setValues] = useState<ArtifactDetailsValues>({
    title: "",
    description: "",
    startPage: "",
  })
  const [result, setResult] = useState<ArtifactPublished | null>(null)
  // The server's normalisation: workspace-relative, no `./`, no trailing `/`.
  const path = (target?.path ?? "").replace(/^(\.\/)+/, "").replace(/\/+$/, "")
  const sourceKey = `${agentId}:${path}`

  const previous = useQuery({
    queryKey: ["artifacts", "mine", "source", sourceKey],
    queryFn: () =>
      ArtifactsService.listArtifacts({ scope: "mine", sourceKey, limit: 1 }),
    enabled: target !== null,
    gcTime: 0,
  })
  const existing = previous.data?.data[0] ?? null
  const ready =
    target !== null && (previous.data !== undefined || previous.isError)

  // Prefill once per open: from the earlier publish, else the basename.
  const [seeded, setSeeded] = useState(false)
  useEffect(() => {
    if (!target || seeded || !ready) return
    const base = path.split("/").pop() || path
    setValues({
      title: existing?.title || (target.isFolder ? base : stripExtension(base)),
      description: existing?.description || "",
      startPage: "",
    })
    setSeeded(true)
  }, [target, seeded, ready, existing, path])

  const mutation = useMutation({
    mutationFn: () =>
      ArtifactsService.publishArtifactFromEnvironment({
        requestBody: {
          environment_id: environmentId,
          path,
          title: values.title.trim(),
          description: values.description.trim() || null,
          entry: values.startPage.trim() || null,
          artifact_id: existing?.id ?? null,
        },
      }),
    onSuccess: (published) => {
      if ((published.excluded ?? []).length === 0) {
        toast.success(`Published ${artifactTitle(published)}`, {
          action: {
            label: "Open",
            onClick: () =>
              navigate({
                to: "/artifact/$artifactId",
                params: { artifactId: published.id },
              }),
          },
        })
        close()
      } else {
        setResult(published)
      }
    },
    onSettled: (published) => invalidateArtifact(queryClient, published?.id),
  })

  function close() {
    setResult(null)
    setSeeded(false)
    mutation.reset()
    onClose()
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (values.title.trim() && !mutation.isPending) mutation.mutate()
  }

  const nextRevision = (existing?.current_revision_number ?? 0) + 1

  return (
    <Dialog
      open={target !== null}
      onOpenChange={(next) => {
        if (!next && !mutation.isPending) close()
      }}
    >
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>Publish as artifact</DialogTitle>
          <DialogDescription className="font-mono text-xs break-all">
            {path}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <PublishResultPanel result={result} onClose={close} />
        ) : !seeded ? (
          <div className="space-y-3">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            {existing && (
              <p className="text-sm text-muted-foreground break-words">
                Updates{" "}
                <span className="font-medium text-foreground">
                  {artifactTitle(existing)}
                </span>{" "}
                — revision {nextRevision} next.
              </p>
            )}
            <ArtifactDetailsFields
              idPrefix="artifact-publish"
              values={values}
              onChange={setValues}
              disabled={mutation.isPending}
              startPage={target?.isFolder ? { mode: "text" } : { mode: "none" }}
            />
            {mutation.isError && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertDescription className="break-words">
                  {publishError(mutation.error)}
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
                disabled={!values.title.trim()}
              >
                {mutation.isPending
                  ? "Publishing…"
                  : existing
                    ? "Update"
                    : "Publish"}
              </LoadingButton>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
