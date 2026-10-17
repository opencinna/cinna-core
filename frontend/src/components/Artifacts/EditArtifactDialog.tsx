import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertCircle } from "lucide-react"
import { type FormEvent, useEffect, useMemo, useState } from "react"

import {
  type ArtifactDetailPublic,
  ArtifactsService,
  type ArtifactUpdate,
} from "@/client"
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
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  ArtifactDetailsFields,
  type ArtifactDetailsValues,
} from "./ArtifactDetailsFields"
import { artifactTitle } from "./artifactFormat"
import { invalidateArtifact } from "./artifactQueries"
import { previewKind } from "./viewers/fileKinds"

interface EditArtifactDialogProps {
  artifact: ArtifactDetailPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

function initialValues(artifact: ArtifactDetailPublic): ArtifactDetailsValues {
  return {
    title: artifact.title,
    description: artifact.description || "",
    startPage: artifact.entry_path || "",
  }
}

/** S7 — rename, describe, or change what opens first. */
export function EditArtifactDialog({
  artifact,
  open,
  onOpenChange,
}: EditArtifactDialogProps) {
  const [saving, setSaving] = useState(false)
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape, outside click and the X wait for a save in flight.
        if (!saving) onOpenChange(next)
      }}
    >
      {open && (
        <EditArtifactForm
          artifact={artifact}
          onDone={() => onOpenChange(false)}
          onSavingChange={setSaving}
        />
      )}
    </Dialog>
  )
}

function EditArtifactForm({
  artifact,
  onDone,
  onSavingChange,
}: {
  artifact: ArtifactDetailPublic
  onDone: () => void
  onSavingChange: (saving: boolean) => void
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()
  const initial = useMemo(() => initialValues(artifact), [artifact])
  const [values, setValues] = useState<ArtifactDetailsValues>(initial)

  // The start page is picked from the current revision's HTML files.
  const htmlFiles = useMemo(() => {
    const current = (artifact.revisions ?? []).find((r) => r.is_current)
    return (current?.files ?? [])
      .filter((f) => previewKind(f) === "html")
      .map((f) => f.path)
      .sort()
  }, [artifact.revisions])

  const changes: ArtifactUpdate = {}
  if (values.title.trim() !== initial.title) changes.title = values.title.trim()
  if (values.description.trim() !== initial.description) {
    changes.description = values.description.trim() || null
  }
  if (values.startPage !== initial.startPage) {
    changes.entry_path = values.startPage || null
  }
  const dirty = Object.keys(changes).length > 0

  const mutation = useMutation({
    mutationFn: (body: ArtifactUpdate) =>
      ArtifactsService.updateArtifact({
        artifactId: artifact.id,
        requestBody: body,
      }),
    onSuccess: (_, body) => {
      showSuccessToast(
        `${artifactTitle({ title: body.title ?? artifact.title })} was updated`,
      )
      onDone()
    },
    onSettled: (_, __, body) =>
      invalidateArtifact(queryClient, artifact.id, {
        views: body.entry_path !== undefined,
      }),
  })

  useEffect(() => {
    onSavingChange(mutation.isPending)
    return () => onSavingChange(false)
  }, [mutation.isPending, onSavingChange])

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (dirty && values.title.trim() && !mutation.isPending) {
      mutation.mutate(changes)
    }
  }

  return (
    <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
      <DialogHeader>
        <DialogTitle>Edit details</DialogTitle>
        <DialogDescription className="break-words">
          {artifactTitle(artifact)}
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={submit} className="space-y-4">
        <ArtifactDetailsFields
          idPrefix="artifact-edit"
          values={values}
          onChange={setValues}
          disabled={mutation.isPending}
          startPage={{ mode: "select", options: htmlFiles }}
        />
        {mutation.isError && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertDescription className="break-words">
              {getErrorMessage(mutation.error, "Couldn't save the details.")}
            </AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={onDone}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            type="submit"
            loading={mutation.isPending}
            disabled={!dirty || !values.title.trim()}
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </LoadingButton>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}
