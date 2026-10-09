import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import { ApiError, type LibraryAgentAdmin, LibraryService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import PendingItems from "@/components/Pending/PendingItems"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { LibraryAccessDialog } from "./LibraryAccessDialog"
import { libraryQueryKeys } from "./queryKeys"
import { RemoveLibraryAgentDialog } from "./RemoveLibraryAgentDialog"
import { teamLibraryColumns } from "./teamLibraryColumns"

function EmptyLibrary() {
  return (
    <div className="flex flex-col items-center justify-center rounded-md border py-16 text-center">
      <p className="max-w-md text-sm text-muted-foreground">
        Agents shared from Cinna Desktop or the CLI appear here.
      </p>
    </div>
  )
}

/**
 * S1 body: every agent in the team library, with the access dialog (S2) and
 * the remove confirm (S3) owned here so a menu item only sets a target.
 */
export function TeamLibraryTable() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [accessTarget, setAccessTarget] = useState<LibraryAgentAdmin | null>(
    null,
  )
  const [deleteTarget, setDeleteTarget] = useState<LibraryAgentAdmin | null>(
    null,
  )
  const [refusal, setRefusal] = useState<string | null>(null)

  const agentsQuery = useQuery({
    queryKey: libraryQueryKeys.admin,
    queryFn: () => LibraryService.listLibraryAdminAgents(),
  })

  const closeDelete = () => {
    setDeleteTarget(null)
    setRefusal(null)
  }

  const deleteMutation = useMutation({
    mutationFn: (agent: LibraryAgentAdmin) =>
      LibraryService.deleteLibraryAgent({ repoId: agent.repo_id }),
    onSuccess: (_data, agent) => {
      showSuccessToast(
        `${agent.name || agent.slug} removed from the team library`,
      )
      closeDelete()
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setRefusal(
          getErrorMessage(err, "This agent is in use and can't be removed."),
        )
        return
      }
      if (err instanceof ApiError && err.status === 404) {
        showErrorToast("This agent was already removed")
        closeDelete()
        return
      }
      showErrorToast(getErrorMessage(err, "Could not remove the agent"))
    },
    onSettled: (_data, _err, agent) => {
      queryClient.invalidateQueries({ queryKey: libraryQueryKeys.admin })
      queryClient.invalidateQueries({
        queryKey: libraryQueryKeys.grants(agent.repo_id),
      })
    },
  })

  const removingId = deleteMutation.isPending
    ? (deleteMutation.variables?.repo_id ?? null)
    : null

  const columns = useMemo(
    () =>
      teamLibraryColumns({
        onEditAccess: setAccessTarget,
        onRemove: (agent) => {
          setRefusal(null)
          setDeleteTarget(agent)
        },
        removingId,
      }),
    [removingId],
  )

  if (agentsQuery.isError) {
    return (
      <QueryErrorAlert
        error={agentsQuery.error}
        fallback="Could not load the team library"
        onRetry={() => agentsQuery.refetch()}
      />
    )
  }
  if (agentsQuery.data === undefined) return <PendingItems />
  // In place of the table rather than inside it: a header row over nothing
  // reads as a table that failed to fill.
  if (agentsQuery.data.length === 0) return <EmptyLibrary />

  return (
    <>
      <DataTable
        columns={columns}
        data={agentsQuery.data}
        getRowId={(agent) => agent.repo_id}
      />
      <LibraryAccessDialog
        agent={accessTarget}
        onClose={() => setAccessTarget(null)}
      />
      <RemoveLibraryAgentDialog
        agent={deleteTarget}
        isPending={deleteMutation.isPending}
        refusal={refusal}
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget)
        }}
        onClose={closeDelete}
      />
    </>
  )
}
