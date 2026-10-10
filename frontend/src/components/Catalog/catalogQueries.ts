import type { QueryClient } from "@tanstack/react-query"

import { catalogItemQueryKey } from "@/utils/catalogItems"

/**
 * After a write to one catalog item: every catalog list (`["catalog-items"]`,
 * the admin scope included), the item's own detail, and the two older reads
 * that still render the same entity — the skill package route
 * (`["skills-catalog"]`) and the cloud install context (`["catalog"]`).
 */
export function invalidateCatalogItem(
  queryClient: QueryClient,
  uuid?: string,
): void {
  queryClient.invalidateQueries({ queryKey: ["catalog-items"] })
  if (uuid) {
    queryClient.invalidateQueries({ queryKey: catalogItemQueryKey(uuid) })
    queryClient.invalidateQueries({ queryKey: ["catalog-item-grants", uuid] })
  }
  queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
  queryClient.invalidateQueries({ queryKey: ["catalog"] })
}
