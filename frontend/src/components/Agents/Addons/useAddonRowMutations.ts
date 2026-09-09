import { useMutation, useQueryClient } from "@tanstack/react-query"

import type { AddonPublic, PluginSyncResponse } from "@/client"
import { LlmPluginsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { addonNoun, invalidateAddons } from "@/utils/addons"

/** A sync result the tab renders in its own dialog when something failed. */
export type SyncReporter = (title: string, result: PluginSyncResponse) => void

/**
 * The three writes an addon row can make, scoped to that row.
 *
 * **Per row, never shared with the card.** A shared `useMutation` describes only
 * its latest call, so a `variables?.id === row.id` gate unfreezes an in-flight
 * row the moment a second row is clicked (memory:
 * `project_shared_mutation_row_pending`); one observer per row cannot get that
 * wrong.
 *
 * All three settle through `invalidateAddons`, which is the only place that
 * knows the projection is a join and therefore which caches a plugin write
 * makes stale. A partial sync failure is handed up rather than toasted: an
 * install that reached three environments and failed on the fourth is not a
 * success with a footnote.
 *
 * Extracted from `AddonRow` because it is the half a second host would copy —
 * it holds no JSX and no row state.
 */
export function useAddonRowMutations(
  agentId: string,
  addon: AddonPublic,
  onSyncResult: SyncReporter,
  onUninstalled: () => void,
) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const link = addon.link ?? null
  const name = addon.display_name
  const noun = addonNoun(addon)
  // Guarded by the caller's `canManage`, which is false without a link; the
  // fallback keeps the call typed without asserting the narrowing away.
  const linkId = link?.id ?? ""

  const settle = () =>
    invalidateAddons(queryClient, agentId, {
      catalog: addon.source === "catalog",
    })

  const settleSync = (
    result: PluginSyncResponse,
    title: string,
    ok: string,
  ) => {
    // `unsupported_syncs` is not folded into `failed_syncs` by the backend —
    // the link write succeeded, so `success` stays true — but an environment
    // that predates the feature did not take the change, so it belongs in the
    // sync-issues dialog and not behind a green toast.
    if (
      (result.failed_syncs && result.failed_syncs > 0) ||
      (result.unsupported_syncs && result.unsupported_syncs > 0) ||
      result.partial_failures
    ) {
      onSyncResult(title, result)
    } else {
      showSuccessToast(ok)
    }
  }

  const update = useMutation({
    mutationFn: (body: {
      conversation_mode?: boolean | null
      building_mode?: boolean | null
      disabled?: boolean | null
    }) =>
      LlmPluginsService.updateAgentPlugin({
        agentId,
        linkId,
        requestBody: body,
      }),
    onSuccess: (result, body) => {
      const title =
        body.disabled != null
          ? body.disabled
            ? `${name} disabled`
            : `${name} enabled`
          : `${name} updated`
      settleSync(result, title, title)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, `Failed to update the ${noun}`)),
    onSettled: settle,
  })

  const upgrade = useMutation({
    mutationFn: () => LlmPluginsService.upgradeAgentPlugin({ agentId, linkId }),
    onSuccess: (result) =>
      settleSync(
        result,
        `${name} updated`,
        `${name} updated to the latest version`,
      ),
    onError: (err) =>
      showErrorToast(getErrorMessage(err, `Failed to update the ${noun}`)),
    onSettled: settle,
  })

  const uninstall = useMutation({
    mutationFn: () =>
      LlmPluginsService.uninstallAgentPlugin({ agentId, linkId }),
    onSuccess: () => {
      showSuccessToast(`${name} uninstalled`)
      onUninstalled()
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, `Failed to uninstall the ${noun}`)),
    onSettled: settle,
  })

  return {
    update,
    upgrade,
    uninstall,
    isPending: update.isPending || upgrade.isPending || uninstall.isPending,
  }
}
