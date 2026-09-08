import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"

import { AdminAiProvidersService, type AIProviderPublic } from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  parseMembersNotRemoved,
  parseProviderDeleteImpact,
  type ProviderDeleteImpact,
} from "@/components/Admin/AiProviders/aiProviderErrors"
import {
  AI_PROVIDERS_QUERY_KEY,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
} from "@/components/Admin/LlmProviders/providerTypes"
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
import { getErrorMessage, handleError } from "@/utils"

interface DeleteProviderDialogProps {
  provider: AIProviderPublic
  /** The vendor's display name, from the adapters endpoint. */
  typeLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Disconnect a provider, having first been told what it costs.
 *
 * **The unforced delete is the probe.** Opening this dialog fires
 * `DELETE /{id}` with no `force`, which is safe by construction: a provider
 * nobody holds a key from is deleted outright (there was nothing to confirm,
 * so the dialog closes with a toast), and one with members is refused with a
 * 409 carrying the impact. Escalate-in-place, the shape
 * `LlmProviderActionsMenu`'s delete confirm already uses.
 *
 * Two things §9 requires of this copy, both load-bearing:
 *
 * 1. **Who loses a key, by name.** A count is not something an administrator
 *    can check before pressing, which is why Phase 4 shaped the impact body
 *    around named people.
 * 2. **Whether the keys are revoked at the vendor**, which depends on the
 *    provider's kind. The surface this replaced promised the opposite — that
 *    live provider keys survive a disconnect — and under §5.4 a forced delete
 *    revokes minted keys first. Carrying that sentence over would have been a
 *    false statement about a destructive action.
 */
export function DeleteProviderDialog({
  provider,
  typeLabel,
  open,
  onOpenChange,
}: DeleteProviderDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [impact, setImpact] = useState<ProviderDeleteImpact | null>(null)
  const [probeError, setProbeError] = useState<string | null>(null)
  // The second 409: the forced delete removed what it could and left the
  // provider standing. Retrying is what completes it, so the dialog stays.
  const [retryNotice, setRetryNotice] = useState<string | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_QUERY_KEY })
    void queryClient.invalidateQueries({
      queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
    })
  }

  const deleteMutation = useMutation({
    mutationFn: (force: boolean) =>
      AdminAiProvidersService.deleteAiProvider({
        providerId: provider.id,
        force,
      }),
    onSuccess: () => {
      showSuccessToast(`Provider "${provider.name}" deleted.`)
      onOpenChange(false)
    },
    onError: (error, force) => {
      const notRemoved = parseMembersNotRemoved(error)
      if (notRemoved) {
        setRetryNotice(notRemoved)
        // The forced delete is *partially destructive before it refuses*: by the
        // time this 409 is raised the minted keys have been revoked and every
        // member that could be removed is gone. The impact on screen is the
        // snapshot from the probe, so leaving it would go on promising to revoke
        // keys that no longer exist. Re-probe: the credential survived, so the
        // unforced call answers with a fresh, smaller impact.
        deleteMutation.mutate(false)
        return
      }
      const refused = parseProviderDeleteImpact(error)
      if (refused) {
        // Only ever the answer to the unforced probe: the forced call does not
        // consult the gate.
        setImpact(refused)
        return
      }
      if (force) {
        handleError.call(showErrorToast, error as ApiError)
        return
      }
      // The probe failed for a reason that is not the gate, so there is no
      // impact to show and nothing safe to offer — say so in place of the list.
      setProbeError(
        getErrorMessage(error, "Couldn't work out what this would cost."),
      )
    },
    onSettled: invalidate,
  })

  const probe = () => {
    setImpact(null)
    setProbeError(null)
    setRetryNotice(null)
    deleteMutation.mutate(false)
  }

  // Fired on open rather than from the menu item: a `DropdownMenuItem` unmounts
  // on select and would take the request's pending state with it.
  //
  // Latched, because this effect sends a real DELETE and StrictMode runs mount
  // effects twice in development — an unlatched probe would issue the
  // destructive request twice, and for a provider with no members the second
  // one would 404 after the first had already deleted it.
  const probedRef = useRef<string | null>(null)
  useEffect(() => {
    if (!open) {
      probedRef.current = null
      return
    }
    if (probedRef.current === provider.id) return
    probedRef.current = provider.id
    probe()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider.id])

  const mintedKeyCount = impact?.minted_key_count ?? 0
  const canForce = impact !== null

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            Delete provider "{provider.name}"?
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">
              {deleteMutation.isPending && !impact ? (
                <p>Working out what this would cost…</p>
              ) : probeError ? (
                <div className="space-y-2">
                  <p className="text-destructive">{probeError}</p>
                  <Button variant="outline" size="sm" onClick={probe}>
                    Retry
                  </Button>
                </div>
              ) : impact ? (
                <>
                  {/* Counted from the list that is actually rendered, so a
                      member the parser dropped as malformed cannot make the
                      sentence promise more names than appear beneath it. */}
                  <p>
                    {impact.members.length}{" "}
                    {impact.members.length === 1
                      ? "person loses"
                      : "people lose"}{" "}
                    the key this provider gave them:
                  </p>
                  <ul className="max-h-[40vh] space-y-1 overflow-y-auto text-sm">
                    {impact.members.map((member) => (
                      <li
                        key={member.user_id}
                        className="rounded-md border bg-muted/30 px-3 py-1.5 text-foreground"
                      >
                        {/* `email` is "" when the user row is missing, and the
                            parser accepts "" as a string — so the id is the
                            last resort rather than an empty bullet. */}
                        {member.full_name || member.email || member.user_id}
                      </li>
                    ))}
                  </ul>
                  {mintedKeyCount > 0 ? (
                    <p>
                      {mintedKeyCount} of those keys{" "}
                      {mintedKeyCount === 1 ? "was" : "were"} created at{" "}
                      {typeLabel} and will be <strong>revoked there</strong>.
                      They stop working immediately.
                    </p>
                  ) : (
                    <p>
                      Their copies of the key are deleted here. The key itself
                      stays valid at {typeLabel} — remove it in your {typeLabel}{" "}
                      console if you no longer want it live.
                    </p>
                  )}
                  {retryNotice && (
                    <p className="text-destructive">{retryNotice}</p>
                  )}
                </>
              ) : (
                // The probe answered 200: nobody held a key, the provider is
                // already gone and `onSuccess` is closing this dialog.
                <p>Nobody holds a key from this provider.</p>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleteMutation.isPending}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            disabled={!canForce || deleteMutation.isPending}
            onClick={(event) => {
              // Kept open while the request is in flight so the pending state
              // and a second refusal both have somewhere to live.
              event.preventDefault()
              setRetryNotice(null)
              deleteMutation.mutate(true)
            }}
          >
            {deleteMutation.isPending && canForce
              ? "Deleting…"
              : retryNotice
                ? "Try again"
                : mintedKeyCount > 0
                  ? `Delete provider and revoke ${mintedKeyCount} ${mintedKeyCount === 1 ? "key" : "keys"}`
                  : "Delete provider"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
