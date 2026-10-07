import { useMutation, useQueryClient } from "@tanstack/react-query"
import { KeyRound } from "lucide-react"
import { useState } from "react"

import type { AgentApiAccessEntry, AgentApiKeyPublic } from "@/client"
import { AgentApiService } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { ApiAccessKeyRow } from "./ApiAccessKeyRow"
import {
  apiAccessPersonLabel,
  pluralize,
  summarizeApiAccessPerson,
} from "./apiAccess"

interface ApiAccessPersonDialogProps {
  agentId: string
  agentName: string
  /** The person, from the card's query (no fetch of its own). */
  entry: AgentApiAccessEntry
  open: boolean
  onOpenChange: (open: boolean) => void
  canGiveKey: boolean
  /** Chained, never nested: the host closes this and opens Give access. */
  onGiveKey: () => void
}

/**
 * S1a — one person's access to this agent's API: their scopes and their keys,
 * and Revoke on a key. Read-only detail otherwise; scopes are edited from the
 * row's menu, a new key is a chained Give-access dialog.
 *
 * Live keys are listed; ended ones (revoked, expired, producer or issuer
 * gone) wait behind one disclosure, kept for the record (plan D27).
 */
export function ApiAccessPersonDialog({
  agentId,
  agentName,
  entry,
  open,
  onOpenChange,
  canGiveKey,
  onGiveKey,
}: ApiAccessPersonDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [showEnded, setShowEnded] = useState(false)
  const [revokeTarget, setRevokeTarget] = useState<AgentApiKeyPublic | null>(
    null,
  )

  const name = apiAccessPersonLabel(entry)
  const scopes = entry.scopes ?? []
  const { liveKeys, endedKeys } = summarizeApiAccessPerson(entry)

  const revokeMutation = useMutation({
    mutationFn: ({ keyId }: { keyId: string }) =>
      AgentApiService.revokeAgentApiKey({ agentId, keyId }),
    onSuccess: () => showSuccessToast("Key revoked"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(agentId),
      })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
  })

  const revokingId = revokeMutation.isPending
    ? revokeMutation.variables?.keyId
    : undefined

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0"
          // The first focusable is a tooltipped control; the body takes focus
          // so no tooltip opens over nothing on mount.
          onOpenAutoFocus={(e) => {
            e.preventDefault()
            ;(e.currentTarget as HTMLElement).focus()
          }}
        >
          <DialogHeader>
            <DialogTitle className="break-words">{name}</DialogTitle>
            <DialogDescription className="break-words">
              {/* The email only when the title is a name — otherwise the
                  title already is the email. */}
              {[
                entry.user.full_name ? entry.user.email : null,
                `API access to ${agentName}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="flex items-start justify-between gap-3">
              <span className="shrink-0 pt-0.5 text-xs text-muted-foreground">
                Scopes
              </span>
              {scopes.length === 0 ? (
                <span className="text-sm">No scopes</span>
              ) : (
                <div className="min-w-0 space-y-1 text-right">
                  <div className="text-sm">
                    {pluralize(scopes.length, "scope", "scopes")}
                  </div>
                  <div className="flex flex-wrap justify-end gap-1">
                    {scopes.map((scope) => (
                      <Badge
                        key={scope}
                        variant="secondary"
                        className="h-5 font-mono text-xs"
                      >
                        {scope}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <h3 className="text-sm font-medium">Keys</h3>
              {liveKeys.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No working keys.
                </p>
              ) : (
                <div className="max-h-[40vh] overflow-y-auto pr-2">
                  <ListRowGroup className="mx-0">
                    {liveKeys.map((key) => (
                      <ApiAccessKeyRow
                        key={key.id}
                        apiKey={key}
                        revoking={revokingId === key.id}
                        onRevoke={() => setRevokeTarget(key)}
                      />
                    ))}
                  </ListRowGroup>
                </div>
              )}

              {endedKeys.length > 0 && (
                <>
                  <Button
                    variant="link"
                    size="sm"
                    className="px-0"
                    aria-expanded={showEnded}
                    onClick={() => setShowEnded((v) => !v)}
                  >
                    {showEnded
                      ? "Hide ended keys"
                      : `Show ${pluralize(endedKeys.length, "ended key", "ended keys")}`}
                  </Button>
                  {showEnded && (
                    <ListRowGroup className="mx-0">
                      {endedKeys.map((key) => (
                        <ApiAccessKeyRow key={key.id} apiKey={key} />
                      ))}
                    </ListRowGroup>
                  )}
                </>
              )}
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              disabled={!canGiveKey}
              onClick={onGiveKey}
            >
              <KeyRound className="mr-1.5 h-4 w-4" />
              Give another key
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={!!revokeTarget}
        onOpenChange={(next) => {
          if (!next) setRevokeTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Revoke {revokeTarget?.label || "this key"} for {name}?
            </AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              Calls with this key stop at once. It stays in {name}'s
              credentials, marked Revoked by issuer, until they delete it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (revokeTarget)
                  revokeMutation.mutate({ keyId: revokeTarget.id })
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
