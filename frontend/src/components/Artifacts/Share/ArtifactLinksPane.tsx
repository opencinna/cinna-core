import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import {
  type ArtifactLinkCreate,
  type ArtifactLinkPublic,
  ArtifactsService,
} from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { Button } from "@/components/ui/button"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ArtifactConfirmDialog } from "../ArtifactConfirmDialog"
import { invalidateArtifact } from "../artifactQueries"
import { ArtifactLinkRow, linkDisplayName } from "./ArtifactLinkRow"
import { CreateLinkForm } from "./CreateLinkForm"

interface ArtifactLinksPaneProps {
  artifactId: string
  /** Not revoked; expired and blocked ones are listed with their state. */
  links: ArtifactLinkPublic[]
  currentRevision: { id: string; number: number } | null
  /** Tells the dialog a create is in flight (Escape must not close it). */
  onBusyChange: (busy: boolean) => void
}

type Mode = "list" | "create"

/**
 * The Public links segment: immediate writes. The list and the create form
 * take turns in one body (§2 Disclosure depth). Creating a link copies its URL
 * at once and returns to the list, where the new row keeps a copy button.
 */
export function ArtifactLinksPane({
  artifactId,
  links,
  currentRevision,
  onBusyChange,
}: ArtifactLinksPaneProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [, copy] = useCopyToClipboard()
  const [mode, setMode] = useState<Mode>("list")
  const listRef = useRef<HTMLDivElement>(null)
  const [focusList, setFocusList] = useState(false)

  // The form's Create button unmounted; land focus on the list, not on a
  // control whose tooltip would open over nothing.
  useEffect(() => {
    if (mode === "list" && focusList) {
      listRef.current?.focus()
      setFocusList(false)
    }
  }, [mode, focusList])
  const [revokeTarget, setRevokeTarget] = useState<ArtifactLinkPublic | null>(
    null,
  )

  const create = useMutation({
    mutationFn: (body: ArtifactLinkCreate) =>
      ArtifactsService.createArtifactLink({ artifactId, requestBody: body }),
    onMutate: () => onBusyChange(true),
    onSuccess: async (link) => {
      const copied = await copy(link.url)
      const code = link.code ? ` Send the code ${link.code} separately.` : ""
      if (copied) showSuccessToast(`Link created and copied.${code}`)
      else showErrorToast(`Link created — copy it from the list.${code}`)
      // Back to the list only once it holds the new row (the toast may send
      // the reader there to copy it).
      await invalidateArtifact(queryClient, artifactId)
      setFocusList(true)
      setMode("list")
    },
    onError: () => invalidateArtifact(queryClient, artifactId),
    onSettled: () => onBusyChange(false),
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

  if (mode === "create") {
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
          setMode("list")
        }}
        onCreate={(body) => create.mutate(body)}
      />
    )
  }

  const createButton = (
    <Button type="button" size="sm" onClick={() => setMode("create")}>
      <Plus className="h-4 w-4" />
      Create link
    </Button>
  )

  return (
    <div ref={listRef} tabIndex={-1} className="space-y-3 outline-none">
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
