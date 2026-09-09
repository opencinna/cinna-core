import { useMutation, useQueryClient } from "@tanstack/react-query"

import { LlmPluginsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"

/**
 * "Sync this marketplace" — the one mutation, for all three places that offer
 * it.
 *
 * Three call sites offer the same verb: the detail page's Configuration tab,
 * the detail page's header menu, and the row menu on the marketplaces list.
 * Each used to carry its own `onSuccess`, and the three had already drifted to
 * three different invalidation sets — the Configuration tab's button (the one
 * the entries table's two empty panels send the admin to) refreshed only the
 * marketplace record, so a re-sync of an already-connected marketplace left
 * the entries table showing the previous sync's Supported reasons and Skills
 * counts until a full page reload. Only the *first* sync appeared to work, and
 * by accident: `status` flipping `pending → connected` mounted the entries
 * query for the first time.
 *
 * So the invalidation set lives here rather than at any call site. One sync
 * moves three readers, and every one of them is a projection of the rows that
 * sync just rewrote:
 *
 * - `["marketplace", id]` — the record itself: status, `last_sync_at`,
 *   `sync_commit_hash`, `plugin_count`.
 * - `["marketplace-plugins", id]` — the entries table. Keyed with a page size
 *   after the id, which prefix-matching covers.
 * - `["marketplaces"]` — the admin list, which prints each marketplace's
 *   status and plugin count.
 */
export function useMarketplaceSync(marketplaceId: string) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  return useMutation({
    mutationFn: () => LlmPluginsService.syncMarketplace({ marketplaceId }),
    onSuccess: () => {
      showSuccessToast("Marketplace synced successfully")
      queryClient.invalidateQueries({
        queryKey: ["marketplace", marketplaceId],
      })
      queryClient.invalidateQueries({
        queryKey: ["marketplace-plugins", marketplaceId],
      })
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
    onError: (error) => {
      showErrorToast(error.message || "Failed to sync marketplace")
    },
  })
}
