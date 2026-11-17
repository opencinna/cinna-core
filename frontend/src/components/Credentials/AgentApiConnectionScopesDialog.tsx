import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import type { ApiError } from "@/client"
import { AgentApiService } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { CONNECTION_SCOPES_EMPTY_HINT } from "@/components/Credentials/AgentApiConnectionFields"
import {
  invalidateConnectionScopeQueries,
  unknownScopeMessage,
} from "@/components/Credentials/agentApiConnectionScopes"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface AgentApiConnectionScopesDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  credentialId: string
  producerAgentId: string
  connectionName: string
  /** The connection's current scopes; the editor is re-seeded on every open. */
  scopes: string[]
}

/**
 * S2e — Edit connection scopes. Catalog-only (the update is strict): a scope
 * the API no longer declares is marked, and Save waits until it's removed.
 * Applies to everyone using the connection from their next call; no token
 * rotation.
 */
export function AgentApiConnectionScopesDialog({
  open,
  onOpenChange,
  credentialId,
  producerAgentId,
  connectionName,
  scopes: currentScopes,
}: AgentApiConnectionScopesDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [scopes, setScopes] = useState<string[]>([])
  const [scopesError, setScopesError] = useState<string | null>(null)

  // Re-seed on every open, so a cancelled edit never survives the next visit.
  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setScopes(currentScopes)
      setScopesError(null)
    }
  }

  const catalogQuery = useQuery({
    queryKey: ["agentApiScopeCatalog", producerAgentId],
    queryFn: () =>
      AgentApiService.getAgentApiScopeCatalog({ agentId: producerAgentId }),
    enabled: open,
  })
  const catalog = catalogQuery.data?.scopes ?? []
  const declared = useMemo(
    () =>
      catalogQuery.isSuccess
        ? new Set((catalogQuery.data?.scopes ?? []).map((s) => s.name))
        : undefined,
    [catalogQuery.isSuccess, catalogQuery.data],
  )
  const undeclaredCount = declared
    ? scopes.filter((s) => !declared.has(s)).length
    : 0

  const saveMutation = useMutation({
    mutationFn: () =>
      AgentApiService.updateAgentApiConnectionScopes({
        agentId: producerAgentId,
        credentialId,
        requestBody: { scopes },
      }),
    onSuccess: () => {
      showSuccessToast("Scopes updated")
      invalidateConnectionScopeQueries(
        queryClient,
        credentialId,
        producerAgentId,
      )
      onOpenChange(false)
    },
    onError: (err) => {
      const message = unknownScopeMessage(err)
      if (message) setScopesError(message)
      else handleError.call(showErrorToast, err as ApiError)
    },
  })

  const isPending = saveMutation.isPending
  const unchanged =
    scopes.length === currentScopes.length &&
    new Set(scopes).size === new Set([...scopes, ...currentScopes]).size
  const canSave =
    catalogQuery.isSuccess && undeclaredCount === 0 && !unchanged && !isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent
        className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0"
        // The first focusable is a chip's remove button, and chips carry
        // tooltips; the body takes focus so none opens on mount.
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement).focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>Edit scopes</DialogTitle>
          <DialogDescription className="break-words">
            Scopes for {connectionName}. They apply to everyone using this
            connection, including people it's shared with and bundles that embed
            it, from their next call.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          <Label>Scopes</Label>
          <AgentApiScopeEditor
            scopes={scopes}
            onChange={(next) => {
              setScopes(next)
              setScopesError(null)
            }}
            catalogScopes={catalog}
            emptyHint={CONNECTION_SCOPES_EMPTY_HINT}
            allowCustom={false}
            declaredScopes={declared}
            disabled={isPending}
          />
          {catalogQuery.isError && (
            <QueryErrorAlert
              error={catalogQuery.error}
              fallback="Couldn't load the scopes this API declares"
              onRetry={() => catalogQuery.refetch()}
              compact
            />
          )}
          {scopesError && (
            <p className="text-xs text-destructive break-words">
              {scopesError}
            </p>
          )}
          {undeclaredCount > 0 && (
            <p className="text-xs text-muted-foreground">
              Remove scopes the API no longer declares to save.
            </p>
          )}
          {catalogQuery.isSuccess && catalog.length === 0 && (
            <p className="text-xs text-muted-foreground">
              This API declares no scopes yet.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={isPending}
            disabled={!canSave}
            onClick={() => saveMutation.mutate()}
          >
            Save scopes
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
