import type { LLMPluginMarketplacePublic } from "@/client"
import { MarketplaceAccessCard } from "./MarketplaceAccessCard"
import { MarketplaceRepositoryCard } from "./MarketplaceRepositoryCard"

interface MarketplaceConfigurationTabProps {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
}

/**
 * The marketplace's Configuration tab: where it syncs from (S13) beside who
 * sees it and its entries (S10).
 */
export function MarketplaceConfigurationTab({
  marketplace,
  marketplaceId,
}: MarketplaceConfigurationTabProps) {
  return (
    <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
      <MarketplaceRepositoryCard
        marketplace={marketplace}
        marketplaceId={marketplaceId}
      />
      <MarketplaceAccessCard marketplace={marketplace} />
    </div>
  )
}
