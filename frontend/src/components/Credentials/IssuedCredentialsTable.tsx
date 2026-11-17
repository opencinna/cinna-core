import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { type ReactNode, useMemo, useState } from "react"

import type { ApiError, IssuedCredentialPublic } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { AgentApiKeyDialog } from "@/components/Credentials/AgentApiKeyDialog"
import { CredentialsEmptySection } from "@/components/Credentials/CredentialsEmptySection"
import {
  getIssuedCredentialColumns,
  isRevokingRow,
} from "@/components/Credentials/issuedCredentialColumns"
import {
  agentQueryKeysForKind,
  ISSUED_KIND_LABEL,
  ISSUED_KIND_LABEL_PLURAL,
  type IssuedKindFilter,
  issuedCredentialLabel,
  issuedHolderLabel,
  matchesIssuedQuery,
  revokeIssuedCredential,
  revokeIssuedCredentialMutationKey,
  type useIssuedCredentials,
} from "@/components/Credentials/issuedCredentials"
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
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const EMPTY_TEXT =
  "Keys and tokens you create so people or outside clients can reach your agents appear here: API keys you give others, A2A tokens and ACP tokens."

interface IssuedCredentialsSectionProps {
  issued: ReturnType<typeof useIssuedCredentials>
  /** The Filters menu's type; "all" for every kind. */
  kind: IssuedKindFilter
  onShowAllTypes: () => void
  /** The page's search, trimmed and lower-cased; `""` for none. */
  query: string
  /** The search as typed, for the no-match sentence. */
  search: string
  onClearSearch: () => void
}

/**
 * The "Issued Credentials" section body: its four states around the table. The
 * empty state's action opens the same Give-access dialog the agent's API
 * access card does, with the agent picker.
 */
export function IssuedCredentialsSection({
  issued,
  kind,
  onShowAllTypes,
  query,
  search,
  onClearSearch,
}: IssuedCredentialsSectionProps) {
  const [giveOpen, setGiveOpen] = useState(false)
  const rows = useMemo(
    () =>
      // Server order (agent, kind, holder, newest first) is kept as is.
      (issued.data?.data ?? []).filter(
        (row) => !query || matchesIssuedQuery(row, query),
      ),
    [issued.data, query],
  )

  if (issued.isError) {
    return (
      <QueryErrorAlert
        error={issued.error}
        fallback="Couldn't load the keys and tokens you issued."
        onRetry={() => issued.refetch()}
      />
    )
  }
  if (issued.data === undefined) return <PendingCredentials />

  const emptyState = query ? (
    <CredentialsEmptySection
      text={`Nothing issued matches "${search.trim()}".`}
    >
      <Button variant="link" size="sm" onClick={onClearSearch}>
        Clear search
      </Button>
    </CredentialsEmptySection>
  ) : kind !== "all" ? (
    <CredentialsEmptySection
      text={`No ${ISSUED_KIND_LABEL_PLURAL[kind]} issued for your agents.`}
    >
      <Button variant="link" size="sm" onClick={onShowAllTypes}>
        Show all types
      </Button>
    </CredentialsEmptySection>
  ) : (
    <CredentialsEmptySection text={EMPTY_TEXT}>
      <Button size="sm" variant="outline" onClick={() => setGiveOpen(true)}>
        Give API access
      </Button>
    </CredentialsEmptySection>
  )

  return (
    <>
      <IssuedCredentialsTable
        rows={rows}
        emptyState={emptyState}
        // A filter switch (live/with ended, workspace) serves placeholder data
        // until the new list lands, so both ends of it restart at page 1.
        resetKey={`${kind}|${query}|${issued.isPlaceholderData}`}
      />
      <AgentApiKeyDialog
        open={giveOpen}
        onOpenChange={setGiveOpen}
        defaultRecipientToMe={false}
      />
    </>
  )
}

/** What a revoke does, per kind, for the confirm's one sentence. */
function revokeSentence(row: IssuedCredentialPublic): string {
  const agent = row.agent?.name || "the agent"
  switch (row.kind) {
    case "a2a_token":
      return `Clients using this token lose access to ${agent} at once.`
    case "acp_token":
      return `The app using this token loses access to ${agent} at once, and a replacement token cannot reopen conversations this one started.`
    default: {
      const holder = issuedHolderLabel(row)
      return `Calls with this key stop at once. It stays in ${holder}'s credentials, marked Revoked by issuer, until they delete it.`
    }
  }
}

interface IssuedCredentialsTableProps {
  /** Already searched by the page, in server order. */
  rows: IssuedCredentialPublic[]
  emptyState: ReactNode
  /** Remount key: a new filter or search starts at page 1. */
  resetKey: string
}

/**
 * "Issued Credentials" on `/credentials`: keys and tokens the caller issued for
 * their agents, one row each. A row opens the agent's Integrations tab, where
 * every kind is managed; The row's ⋯ menu offers Open in agent, Copy ID and
 * Revoke (confirmed, list-level target so the confirm outlives the menu).
 * No reveal or rotate — the issuer does not hold the value here.
 */
export function IssuedCredentialsTable({
  rows,
  emptyState,
  resetKey,
}: IssuedCredentialsTableProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [revokeTarget, setRevokeTarget] =
    useState<IssuedCredentialPublic | null>(null)

  const revokeMutation = useMutation({
    mutationKey: revokeIssuedCredentialMutationKey,
    mutationFn: (row: IssuedCredentialPublic) => revokeIssuedCredential(row),
    onSuccess: (_res, row) =>
      showSuccessToast(`${ISSUED_KIND_LABEL[row.kind]} revoked`),
    onError: (err) => handleError.bind(showErrorToast)(err as ApiError),
    onSettled: (_res, _err, row) => {
      // `["credentials"]` covers this list and the holder-side rows.
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      if (row.agent) {
        for (const queryKey of agentQueryKeysForKind(row.kind, row.agent.id)) {
          queryClient.invalidateQueries({ queryKey })
        }
      }
    },
  })

  const columns = useMemo(
    () => getIssuedCredentialColumns({ onRevoke: setRevokeTarget }),
    [],
  )

  const label = revokeTarget ? issuedCredentialLabel(revokeTarget) : ""
  const noun = revokeTarget ? ISSUED_KIND_LABEL[revokeTarget.kind] : ""

  return (
    <>
      <DataTable
        key={resetKey}
        autoResetPageIndex={false}
        columns={columns}
        data={rows}
        getRowId={(row) => row.id}
        tableClassName="table-fixed"
        itemLabel="keys and tokens"
        emptyState={emptyState}
        onRowClick={(row) => {
          if (!row.agent) return
          // A row whose revoke is in flight is not a way into it.
          if (
            queryClient.isMutating({
              mutationKey: revokeIssuedCredentialMutationKey,
              predicate: (m) => isRevokingRow(m.state.variables, row.id),
            }) > 0
          ) {
            return
          }
          navigate({
            to: "/agent/$agentId",
            params: { agentId: row.agent.id },
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
              Revoke {label}
              {revokeTarget?.holder_kind === "person"
                ? ` for ${issuedHolderLabel(revokeTarget)}`
                : ""}
              ?
            </AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              {revokeTarget ? revokeSentence(revokeTarget) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (revokeTarget) revokeMutation.mutate(revokeTarget)
                setRevokeTarget(null)
              }}
            >
              Revoke {noun}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
