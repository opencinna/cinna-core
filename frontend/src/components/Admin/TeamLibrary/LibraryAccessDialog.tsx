import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { type ReactNode, useState } from "react"

import {
  ApiError,
  type LibraryAgentAdmin,
  type LibraryGrantIn,
  LibraryService,
} from "@/client"
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
import { LibraryAccessForm } from "./LibraryAccessForm"
import { libraryQueryKeys } from "./queryKeys"

interface LibraryAccessDialogProps {
  /** The agent whose access is edited; `null` closes the dialog. */
  agent: LibraryAgentAdmin | null
  onClose: () => void
}

/**
 * S2 — who can get one team-library agent, who can share changes to it and
 * who maintains it. Titled by the agent; the body is a buffered form whose
 * Save replaces the whole grant list.
 */
export function LibraryAccessDialog({
  agent,
  onClose,
}: LibraryAccessDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [refusal, setRefusal] = useState<string | null>(null)
  const repoId = agent?.repo_id ?? ""
  const name = agent ? agent.name || agent.slug : ""

  const grantsQuery = useQuery({
    queryKey: libraryQueryKeys.grants(repoId),
    queryFn: () => LibraryService.getLibraryAgentGrants({ repoId }),
    enabled: agent !== null,
    // The form seeds its buffer once from this data, so it must never be a
    // cached copy that a background refetch later replaces: Save would then
    // PUT the stale list over newer grants. No cache across opens, and no
    // refetch under an open form.
    gcTime: 0,
    refetchOnWindowFocus: false,
  })

  const saveMutation = useMutation({
    mutationFn: (grants: LibraryGrantIn[]) =>
      LibraryService.putLibraryAgentGrants({ repoId, requestBody: grants }),
    onSuccess: () => {
      showSuccessToast(`Access to ${name} updated`)
      close()
    },
    onError: (err) => {
      // A refusal is a sentence about this list (no maintainer, Maintainer on
      // a role) — it belongs beside the list, with the edits kept.
      if (err instanceof ApiError) {
        if (err.status === 404) {
          showErrorToast("This agent was already removed")
          close()
          return
        }
        const detail = (err.body as { detail?: unknown } | undefined)?.detail
        if (
          (err.status === 409 || err.status === 422) &&
          typeof detail === "string"
        ) {
          setRefusal(detail || "Could not save access")
          return
        }
      }
      showErrorToast(getErrorMessage(err, "Could not save access"))
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: libraryQueryKeys.admin })
      queryClient.invalidateQueries({
        queryKey: libraryQueryKeys.grants(repoId),
      })
    },
  })

  function close() {
    setRefusal(null)
    saveMutation.reset()
    onClose()
  }

  let body: ReactNode
  if (grantsQuery.isError) {
    body = (
      <>
        <QueryErrorAlert
          error={grantsQuery.error}
          fallback="Could not load access"
          onRetry={() => grantsQuery.refetch()}
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
        </DialogFooter>
      </>
    )
  } else if (grantsQuery.data === undefined) {
    body = (
      <>
        <div className="space-y-3">
          <h3 className="text-sm font-medium">Access</h3>
          <div className="space-y-1">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-[40px] w-full rounded-md" />
            ))}
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button type="button" disabled>
            Save
          </Button>
        </DialogFooter>
      </>
    )
  } else {
    body = (
      <LibraryAccessForm
        // Re-seed the buffer when the dialog moves to another agent.
        key={repoId}
        grants={grantsQuery.data}
        isSaving={saveMutation.isPending}
        refusal={refusal}
        onEdited={() => setRefusal(null)}
        onSave={(grants) => saveMutation.mutate(grants)}
        onCancel={close}
      />
    )
  }

  return (
    <Dialog
      open={agent !== null}
      onOpenChange={(next) => {
        if (!next && !saveMutation.isPending) close()
      }}
    >
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">{name}</DialogTitle>
          <DialogDescription className="break-words">
            {agent?.description ||
              "Choose who can get this agent from the team library and who can change it."}
          </DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  )
}
