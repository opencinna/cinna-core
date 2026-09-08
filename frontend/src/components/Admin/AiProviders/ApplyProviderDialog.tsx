import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"

import {
  AdminAiProvidersService,
  type AIProviderPublic,
  type ManagedAICredentialApplyResult,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  AI_PROVIDERS_QUERY_KEY,
  knownSdkModes,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  sdkModeLabel,
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
import { handleError } from "@/utils"
import { userRoleLabel } from "@/utils/userRoles"

interface ApplyProviderDialogProps {
  provider: AIProviderPublic
  typeLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Plain-English list, e.g. "Agent User and Agent Developer". */
function joinLabels(labels: string[]): string {
  if (labels.length === 0) return ""
  if (labels.length === 1) return labels[0]
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`
}

/**
 * Grant this provider to every existing account whose role it covers.
 *
 * `auto_provision_roles` only fires when an account is created, so this is the
 * explicit path for the people already on the instance — and it is one of the
 * two deliberate admin acts that *do* overwrite a default somebody already
 * holds (§5.5). The dialog therefore previews with a dry run before it commits.
 *
 * Moved here from `LlmProviders/LlmProviderActionsMenu.tsx`: §5.2 moved the
 * action onto the provider, and the managed-credential route it used to call
 * is gone. A manual managed credential has no roles left to apply, so the
 * action does not exist on that surface at all any more.
 *
 * **Where each number comes from.** The counts are the dry run's, always: the
 * row this dialog hangs off is up to 30s stale and the candidate set moves on
 * its own. The *policy* the copy narrates — which roles, which default slots —
 * is read from the provider row, because the apply result projects the managed
 * credential, and the credential stopped carrying the rule when the rule moved
 * to the provider. So a policy edit made in another tab between the row's last
 * refetch and this press would be narrated from the older configuration while
 * the counts came from the newer one; the server is the authority either way,
 * and the count is the number the button quotes.
 */
export function ApplyProviderDialog({
  provider,
  typeLabel,
  open,
  onOpenChange,
}: ApplyProviderDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_QUERY_KEY })
    void queryClient.invalidateQueries({
      queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
    })
    for (const queryKey of [
      ["currentUser"],
      ["aiCredentialsList"],
      ["aiCredentialsStatus"],
      ["resolveDefaultCredential"],
    ]) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }

  // Two mutations rather than one with a `dryRun` flag: the preview has to stay
  // on screen while the real run is in flight, so its result cannot be the same
  // piece of state the commit overwrites.
  const previewMutation = useMutation<ManagedAICredentialApplyResult>({
    mutationFn: () =>
      AdminAiProvidersService.applyAiProviderToExisting({
        providerId: provider.id,
        dryRun: true,
      }),
  })

  const applyMutation = useMutation<ManagedAICredentialApplyResult, ApiError>({
    mutationFn: () =>
      AdminAiProvidersService.applyAiProviderToExisting({
        providerId: provider.id,
        dryRun: false,
      }),
    onSuccess: (result) => {
      const added = result.added ?? []
      const skipped = result.skipped ?? []
      // A skipped account is by definition not a member, so the provider's own
      // projection cannot name them. The preview can: it listed every candidate
      // with their email before any of this was attempted.
      const labelById = new Map<string, string>()
      for (const candidate of previewMutation.data?.candidates ?? []) {
        labelById.set(
          candidate.user_id,
          candidate.full_name
            ? `${candidate.full_name} <${candidate.email}>`
            : candidate.email,
        )
      }
      // One toast per skip, by name: a summary count would tell the admin that
      // somebody was left out without telling them who to go and fix.
      for (const skip of skipped) {
        showErrorToast(
          `${labelById.get(skip.user_id) ?? skip.user_id} skipped (${skip.reason}).`,
        )
      }
      // The preview is a snapshot; nothing locks the candidate set between the
      // dry run and this commit, and a signup in between is exactly the kind of
      // thing this feature exists alongside.
      const drifted = result.candidate_count !== candidateCount
      if (added.length === 0 && skipped.length === 0) {
        showSuccessToast("Nobody was missing this key — nothing changed.")
      } else if (added.length > 0) {
        showSuccessToast(
          drifted
            ? `Granted to ${added.length} ${added.length === 1 ? "account" : "accounts"} — ${candidateCount} were previewed, the set changed in between.`
            : `Granted to ${added.length} ${added.length === 1 ? "account" : "accounts"}.`,
        )
      }
      onOpenChange(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: invalidate,
  })

  // Always re-previewed on open, never decided from the row: the roles and the
  // membership move on their own, and deciding from a stale row is how the
  // dialog ends up telling an admin a provider grants to nobody while the
  // server says otherwise.
  useEffect(() => {
    if (!open) return
    previewMutation.reset()
    applyMutation.reset()
    previewMutation.mutate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider.id])

  const preview = previewMutation.data
  const autoRoles = provider.auto_provision_roles ?? []
  // Every default slot this provider takes over, named the way the admin reads
  // them. Both flags are counted by `_count_default_overwrites` and both have
  // to appear: they are independent, and a dialog that described only the
  // SDK-defaults axis said nothing at all about a provider whose only
  // default-writing flag is `set_as_default`.
  const wiredModes = provider.set_user_sdk_defaults
    ? knownSdkModes(provider.sdk_default_modes)
    : []
  const claimedSlots = [
    ...wiredModes.map((mode) => `the ${sdkModeLabel(mode).toLowerCase()} default`),
    ...(provider.set_as_default ? [`the default ${typeLabel} key`] : []),
  ]
  const claimedSlotsPhrase = joinLabels(claimedSlots)

  const candidateCount = preview?.candidate_count ?? 0
  const overwriteCount = preview?.defaults_overwrite_count ?? 0
  const canApply = autoRoles.length > 0 && !!preview && candidateCount > 0

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Apply to existing users</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">
              {previewMutation.isPending ? (
                <p>Working out who would receive this key…</p>
              ) : previewMutation.isError ? (
                <div className="space-y-2">
                  <p>Couldn't work out who would receive this key.</p>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => previewMutation.mutate()}
                  >
                    Retry
                  </Button>
                </div>
              ) : autoRoles.length === 0 ? (
                <p>
                  "{provider.name}" isn't set to auto-provision for any role, so
                  there is nobody to apply it to. Pick roles under Edit provider
                  first.
                </p>
              ) : candidateCount === 0 ? (
                // A zero means "nobody is missing it" — which covers both
                // "everyone already has it" and "no account holds these roles
                // at all". The response cannot tell them apart, so the sentence
                // must not pick one.
                <p>
                  No active {joinLabels(autoRoles.map(userRoleLabel))} account is
                  missing "{provider.name}" right now — nothing to apply.
                </p>
              ) : (
                <>
                  <p>
                    <strong>{candidateCount}</strong>{" "}
                    {candidateCount === 1 ? "account" : "accounts"} (
                    {joinLabels(autoRoles.map(userRoleLabel))}) will receive "
                    {provider.name}".
                  </p>
                  {overwriteCount > 0 ? (
                    <p>
                      <strong>{overwriteCount}</strong> of them already{" "}
                      {overwriteCount === 1 ? "has" : "have"} a default this
                      grant takes away
                      {claimedSlots.length > 0
                        ? `. "${provider.name}" becomes ${claimedSlotsPhrase} for every account here`
                        : ""}
                      {wiredModes.length > 0
                        ? ", and the model each of those modes was pinned to is reset."
                        : "."}
                    </p>
                  ) : (
                    claimedSlots.length > 0 && (
                      <p>
                        None of them has picked a key or a model for{" "}
                        {claimedSlotsPhrase} yet, so "{provider.name}" fills{" "}
                        {claimedSlots.length === 1 ? "it" : "those"} in rather
                        than replacing a choice.
                      </p>
                    )
                  )}
                  <p>
                    Add-only — nobody loses a key, and current members are left
                    alone.
                  </p>
                </>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={applyMutation.isPending}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault()
              applyMutation.mutate()
            }}
            disabled={!canApply || applyMutation.isPending}
          >
            {applyMutation.isPending
              ? "Applying…"
              : canApply
                ? `Grant to ${candidateCount} ${candidateCount === 1 ? "account" : "accounts"}`
                : "Grant"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
