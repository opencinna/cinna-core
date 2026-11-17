import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { type ReactNode, useMemo, useState } from "react"

import {
  AgentApiService,
  type ApiError,
  type IssuedAgentApiKeyPublic,
} from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { AgentApiKeyDialog } from "@/components/Credentials/AgentApiKeyDialog"
import { CredentialsEmptySection } from "@/components/Credentials/CredentialsEmptySection"
import { getIssuedKeyColumns } from "@/components/Credentials/issuedKeyColumns"
import {
  issuedKeyLabel,
  issuedKeyRecipientLabel,
  matchesIssuedKeyQuery,
  revokeIssuedKeyMutationKey,
  type useIssuedAgentApiKeys,
} from "@/components/Credentials/issuedKeys"
import PendingCredentials from "@/components/Pending/PendingCredentials"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface RevokeVars {
  keyId: string
  producerId: string
}

interface IssuedKeysSectionProps {
  issued: ReturnType<typeof useIssuedAgentApiKeys>
  /** The page's search, trimmed and lower-cased; `""` for none. */
  query: string
  /** The search as typed, for the no-match sentence. */
  search: string
  onClearSearch: () => void
}

/**
 * The "Issued Credentials" section body: its four states around the table. The
 * empty state's action opens the same Give-access dialog the producer's API
 * access card does, with the producer picker.
 */
export function IssuedKeysSection({
  issued,
  query,
  search,
  onClearSearch,
}: IssuedKeysSectionProps) {
  const [giveOpen, setGiveOpen] = useState(false)
  const rows = useMemo(
    () =>
      // Server order (producer, recipient, newest first) is kept as is.
      (issued.data?.data ?? []).filter(
        (key) => !query || matchesIssuedKeyQuery(key, query),
      ),
    [issued.data, query],
  )

  if (issued.isError) {
    return (
      <QueryErrorAlert
        error={issued.error}
        fallback="Couldn't load the keys you issued."
        onRetry={() => issued.refetch()}
      />
    )
  }
  if (issued.data === undefined) return <PendingCredentials />

  const emptyState = query ? (
    <CredentialsEmptySection text={`No issued keys match "${search.trim()}".`}>
      <Button variant="link" size="sm" onClick={onClearSearch}>
        Clear search
      </Button>
    </CredentialsEmptySection>
  ) : (
    <CredentialsEmptySection text="API keys you give other people from an agent's API access card are listed here — they live in each recipient's credentials, not yours.">
      <Button size="sm" variant="outline" onClick={() => setGiveOpen(true)}>
        Give API access
      </Button>
    </CredentialsEmptySection>
  )

  return (
    <>
      <IssuedKeysTable rows={rows} emptyState={emptyState} resetKey={query} />
      <AgentApiKeyDialog
        open={giveOpen}
        onOpenChange={setGiveOpen}
        defaultRecipientToMe={false}
      />
    </>
  )
}

interface IssuedKeysTableProps {
  /** Already searched and sorted by the page. */
  rows: IssuedAgentApiKeyPublic[]
  emptyState: ReactNode
  /** Remount key: a new search starts at page 1. */
  resetKey: string
}

/**
 * "Issued Credentials" on `/credentials`: keys the caller gave other people,
 * one row each. A row opens the producer's API access card, where the key is
 * managed; Revoke is the one inline action (confirmed, list-level target so
 * the confirm outlives the row's hover-revealed button). No reveal or rotate —
 * the issuer never holds the value.
 */
export function IssuedKeysTable({
  rows,
  emptyState,
  resetKey,
}: IssuedKeysTableProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [revokeTarget, setRevokeTarget] =
    useState<IssuedAgentApiKeyPublic | null>(null)

  const revokeMutation = useMutation({
    mutationKey: revokeIssuedKeyMutationKey,
    mutationFn: ({ keyId, producerId }: RevokeVars) =>
      AgentApiService.revokeAgentApiKey({ agentId: producerId, keyId }),
    onSuccess: () => showSuccessToast("Key revoked"),
    onError: (err) => handleError.bind(showErrorToast)(err as ApiError),
    onSettled: (_res, _err, { producerId }) => {
      // `["credentials"]` covers this list and the recipient-side rows.
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(producerId),
      })
    },
  })

  const columns = useMemo(
    () => getIssuedKeyColumns({ onRevoke: setRevokeTarget }),
    [],
  )

  const recipient = revokeTarget ? issuedKeyRecipientLabel(revokeTarget) : ""

  return (
    <>
      <DataTable
        key={resetKey}
        autoResetPageIndex={false}
        columns={columns}
        data={rows}
        getRowId={(row) => row.id}
        tableClassName="table-fixed"
        hideHeader
        itemLabel="keys"
        emptyState={emptyState}
        onRowClick={(row) => {
          if (!row.producer) return
          // A row whose revoke is in flight is not a way into it.
          if (
            queryClient.isMutating({
              mutationKey: revokeIssuedKeyMutationKey,
              predicate: (m) =>
                (m.state.variables as RevokeVars | undefined)?.keyId === row.id,
            }) > 0
          ) {
            return
          }
          navigate({
            to: "/agent/$agentId",
            params: { agentId: row.producer.id },
            hash: "integrations",
          })
        }}
      />

      <AlertDialog
        open={!!revokeTarget}
        onOpenChange={(next) => {
          if (!next) setRevokeTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Revoke {revokeTarget ? issuedKeyLabel(revokeTarget) : "this key"}{" "}
              for {recipient}?
            </AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              Calls with this key stop at once. It stays in {recipient}'s
              credentials, marked Revoked by issuer, until they delete it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (revokeTarget?.producer) {
                  revokeMutation.mutate({
                    keyId: revokeTarget.id,
                    producerId: revokeTarget.producer.id,
                  })
                }
                setRevokeTarget(null)
              }}
            >
              Revoke key
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
