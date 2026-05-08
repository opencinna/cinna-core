/**
 * useInstallContext — install-page data hook.
 *
 * Calls ``GET /catalog/{bundle_id}/install-context`` and caches it under
 * ``["catalog", bundleId, "install-context"]``. The response gives the
 * single-screen install page everything it needs in one round-trip:
 * the catalog entry, publisher AI credential summaries, and per-spec
 * auto-prefill suggestions.
 */
import { useQuery } from "@tanstack/react-query"

import { CatalogService } from "@/client"

export function useInstallContext(bundleId: string) {
  return useQuery({
    queryKey: ["catalog", bundleId, "install-context"],
    queryFn: () => CatalogService.getInstallContext({ bundleId }),
    enabled: Boolean(bundleId),
  })
}
