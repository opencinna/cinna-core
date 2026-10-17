import { useMutation, useQueryClient } from "@tanstack/react-query"

import { type ArtifactPublic, ArtifactsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ArtifactConfirmDialog } from "./ArtifactConfirmDialog"
import { artifactTitle, plural } from "./artifactFormat"
import {
  deleteArtifactMutationKey,
  invalidateArtifact,
} from "./artifactQueries"

interface DeleteArtifactDialogProps {
  /** The artifact to delete; `null` closes the dialog. */
  artifact: Pick<
    ArtifactPublic,
    "id" | "title" | "revision_count" | "live_link_count"
  > | null
  onClose: () => void
  /** After the delete lands (the page navigates away; the list does nothing). */
  onDeleted?: () => void
}

/** S8 — "Delete artifact <title>?", stating what dies with it. */
export function DeleteArtifactDialog({
  artifact,
  onClose,
  onDeleted,
}: DeleteArtifactDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const id = artifact?.id ?? ""

  const mutation = useMutation({
    mutationKey: deleteArtifactMutationKey(id),
    mutationFn: () => ArtifactsService.deleteArtifact({ artifactId: id }),
    onSuccess: () => {
      showSuccessToast(`${artifactTitle(artifact ?? {})} was deleted`)
      onClose()
      onDeleted?.()
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Couldn't delete the artifact")),
    // Returned, so the row stays pending until the list has refetched.
    onSettled: () => invalidateArtifact(queryClient),
  })

  const revisions = artifact?.revision_count ?? 1
  const links = artifact?.live_link_count ?? 0

  return (
    <ArtifactConfirmDialog
      open={artifact !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={
        <>
          Delete artifact <em>{artifactTitle(artifact ?? {})}</em>?
        </>
      }
      description={`${
        revisions === 1
          ? "Its revision and files are removed"
          : `All ${plural(revisions, "revision")} and their files are removed`
      }${
        links > 0
          ? `, and its ${plural(links, "public link")} stop${links === 1 ? "s" : ""} working immediately`
          : ""
      }. This can't be undone.`}
      actionLabel="Delete artifact"
      pendingLabel="Deleting…"
      pending={mutation.isPending}
      onConfirm={() => mutation.mutate()}
    />
  )
}
