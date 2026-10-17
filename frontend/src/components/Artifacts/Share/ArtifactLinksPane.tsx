import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"

import {
  type ArtifactLinkCreate,
  type ArtifactLinkCreated,
  type ArtifactLinkPublic,
  ArtifactsService,
} from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ArtifactConfirmDialog } from "../ArtifactConfirmDialog"
import { invalidateArtifact } from "../artifactQueries"
import { ArtifactLinkRow, linkDisplayName } from "./ArtifactLinkRow"
import { CreateLinkForm } from "./CreateLinkForm"
import { LinkCreatedPanel } from "./LinkCreatedPanel"

interface ArtifactLinksPaneProps {
  artifactId: string
  /** Not revoked; expired and blocked ones are listed with their state. */
  links: ArtifactLinkPublic[]
  currentRevision: { id: string; number: number } | null
  /** Tells the dialog a create is in flight (Escape must not close it). */
  onBusyChange: (busy: boolean) => void
}

type Mode =
  | { kind: "list" }
  | { kind: "create" }
  | { kind: "created"; link: ArtifactLinkCreated }

/**
 * The Public links segment: immediate writes. The list, the create form and
 * the one-time result take turns in one body (§2 Disclosure depth).
 */
export function ArtifactLinksPane({
  artifactId,
  links,
  currentRevision,
  onBusyChange,
}: ArtifactLinksPaneProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [mode, setMode] = useState<Mode>({ kind: "list" })
  const [revokeTarget, setRevokeTarget] = useState<ArtifactLinkPublic | null>(
    null,
  )

  const create = useMutation({
    mutationFn: (body: ArtifactLinkCreate) =>
      ArtifactsService.createArtifactLink({ artifactId, requestBody: body }),
    onMutate: () => onBusyChange(true),
    onSuccess: (link) => setMode({ kind: "created", link }),
    onSettled: () => {
      onBusyChange(false)
      return invalidateArtifact(queryClient, artifactId)
    },
  })

  const revoke = useMutation({
    mutationFn: (link: ArtifactLinkPublic) =>
      ArtifactsService.revokeArtifactLink({ artifactId, linkId: link.id }),
    onSuccess: (_, link) => {
      showSuccessToast(`${linkDisplayName(link)} was revoked`)
      setRevokeTarget(null)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Couldn't revoke the link")),
    onSettled: () => invalidateArtifact(queryClient, artifactId),
  })

  if (mode.kind === "create") {
    return (
      <CreateLinkForm
        currentRevision={currentRevision}
        isCreating={create.isPending}
        error={
          create.isError
            ? getErrorMessage(create.error, "Couldn't create the link")
            : null
        }
        onBack={() => {
          create.reset()
          setMode({ kind: "list" })
        }}
        onCreate={(body) => create.mutate(body)}
      />
    )
  }

  if (mode.kind === "created") {
    return (
      <LinkCreatedPanel
        link={mode.link}
        onDone={() => {
          create.reset()
          setMode({ kind: "list" })
        }}
      />
    )
  }

  const createButton = (
    <Button type="button" size="sm" onClick={() => setMode({ kind: "create" })}>
      <Plus className="h-4 w-4" />
      Create link
    </Button>
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Anyone with a link can open this artifact without signing in.
        </p>
        {links.length > 0 && createButton}
      </div>
      {links.length === 0 ? (
        <div className="flex flex-col items-center gap-3 py-4">
          <p className="text-sm text-muted-foreground">No public links yet.</p>
          {createButton}
        </div>
      ) : (
        <div className="max-h-[40vh] overflow-y-auto pr-2">
          <ListRowGroup className="mx-0">
            {links.map((link) => (
              <ArtifactLinkRow
                key={link.id}
                link={link}
                revoking={revoke.isPending && revoke.variables?.id === link.id}
                onRevoke={setRevokeTarget}
              />
            ))}
          </ListRowGroup>
        </div>
      )}

      <ArtifactConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null)
        }}
        title={
          <>
            Revoke link{" "}
            <em>{revokeTarget ? linkDisplayName(revokeTarget) : ""}</em>?
          </>
        }
        description="Anyone using it loses access immediately."
        actionLabel="Revoke link"
        pendingLabel="Revoking…"
        pending={revoke.isPending}
        onConfirm={() => revokeTarget && revoke.mutate(revokeTarget)}
      />
    </div>
  )
}
