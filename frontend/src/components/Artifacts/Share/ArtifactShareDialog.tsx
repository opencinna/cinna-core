import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { ApiError, type ArtifactDetailPublic, ArtifactsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ArtifactSegments } from "../ArtifactSegments"
import { artifactTitle } from "../artifactFormat"
import { artifactKeys, invalidateArtifact } from "../artifactQueries"
import { ArtifactLinksPane } from "./ArtifactLinksPane"
import {
  ArtifactPeopleForm,
  type ArtifactPeopleSave,
} from "./ArtifactPeopleForm"

type ShareSegment = "people" | "links"

interface ArtifactShareDialogProps {
  /** The artifact to share; `null` closes the dialog. */
  artifact: { id: string; title: string } | null
  onClose: () => void
}

function SkeletonRows() {
  return (
    <div className="space-y-1">
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-[40px] w-full rounded-md" />
      ))}
    </div>
  )
}

/**
 * S6 — who can open an artifact. Two segments because they are two
 * interaction models: People is a buffered Save form (the catalog dialog's
 * shape), Public links are immediate create / revoke.
 */
export function ArtifactShareDialog({
  artifact,
  onClose,
}: ArtifactShareDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [segment, setSegment] = useState<ShareSegment>("people")
  const [refusal, setRefusal] = useState<string | null>(null)
  const [linksBusy, setLinksBusy] = useState(false)
  const id = artifact?.id ?? ""

  const detail = useQuery({
    queryKey: artifactKeys.detail(id),
    queryFn: () => ArtifactsService.getArtifact({ artifactId: id }),
    enabled: artifact !== null,
    // The people buffer is seeded from a read made *after* opening, never a
    // cached copy, so Save cannot PUT an old list over newer grants.
    refetchOnMount: "always",
  })

  const [seed, setSeed] = useState<ArtifactDetailPublic | null>(null)
  useEffect(() => {
    if (
      !seed &&
      detail.data &&
      detail.isFetchedAfterMount &&
      !detail.isFetching
    ) {
      setSeed(detail.data)
    }
  }, [seed, detail.data, detail.isFetchedAfterMount, detail.isFetching])

  const save = useMutation({
    mutationFn: async (next: ArtifactPeopleSave) => {
      const putGrants = () =>
        ArtifactsService.replaceArtifactGrants({
          artifactId: id,
          requestBody: next.grants,
        })
      const patchVisibility = () =>
        ArtifactsService.updateArtifact({
          artifactId: id,
          requestBody: { visibility: next.visibility },
        })
      // Narrowing to "Only me": the named people exist before everyone else
      // loses access; widening: everyone gains it after the list is final.
      if (next.visibility === "private") {
        if (next.grantsChanged) await putGrants()
        if (next.visibilityChanged) await patchVisibility()
      } else {
        if (next.visibilityChanged) await patchVisibility()
        if (next.grantsChanged) await putGrants()
      }
    },
    onSuccess: () => {
      showSuccessToast(`Access to ${artifactTitle(artifact ?? {})} updated`)
      close()
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        if (err.status === 404) {
          showErrorToast("This artifact was already deleted")
          close()
          return
        }
        if (err.status === 409 || err.status === 422) {
          setRefusal(getErrorMessage(err, "Couldn't save access"))
          return
        }
      }
      showErrorToast(getErrorMessage(err, "Couldn't save access"))
    },
    onSettled: () => invalidateArtifact(queryClient, id),
  })

  function close() {
    setRefusal(null)
    setSeed(null)
    setSegment("people")
    save.reset()
    onClose()
  }

  const busy = save.isPending || linksBusy
  const links = (detail.data?.links ?? []).filter((l) => !l.revoked_at)
  const current = detail.data?.current_revision_id
    ? {
        id: detail.data.current_revision_id,
        number: detail.data.current_revision_number ?? 1,
      }
    : null

  let body: React.ReactNode
  if (detail.isError && !seed) {
    body = (
      <>
        <QueryErrorAlert
          error={detail.error}
          fallback="Couldn't load who can open this artifact."
          onRetry={() => detail.refetch()}
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
        </DialogFooter>
      </>
    )
  } else {
    body = (
      <>
        <ArtifactSegments
          label="Share"
          value={segment}
          onChange={setSegment}
          segments={[
            { value: "people", label: "People" },
            {
              value: "links",
              label: "Public links",
              count: detail.data ? links.length : null,
            },
          ]}
        />
        {/* People stays mounted while Links is shown: its buffer survives. */}
        <div className={segment === "people" ? "space-y-4" : "hidden"}>
          {seed ? (
            <ArtifactPeopleForm
              grants={seed.grants ?? []}
              visibility={seed.visibility}
              isSaving={save.isPending}
              refusal={refusal}
              onEdited={() => setRefusal(null)}
              onSave={(next) => save.mutate(next)}
              onCancel={close}
            />
          ) : (
            <SkeletonRows />
          )}
        </div>
        {segment === "links" &&
          (detail.data ? (
            <ArtifactLinksPane
              artifactId={id}
              links={links}
              currentRevision={current}
              onBusyChange={setLinksBusy}
            />
          ) : (
            <SkeletonRows />
          ))}
      </>
    )
  }

  return (
    <Dialog
      open={artifact !== null}
      onOpenChange={(next) => {
        if (!next && !busy) close()
      }}
    >
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            {artifactTitle(artifact ?? {})}
          </DialogTitle>
          <DialogDescription>
            Choose who can open this artifact.
          </DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  )
}
