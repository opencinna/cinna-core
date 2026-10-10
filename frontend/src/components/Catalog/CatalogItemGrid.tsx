/**
 * CatalogItemGrid — the responsive, equal-height grid of catalog tiles.
 */
import type { CatalogItem } from "@/client"

import { CatalogItemCard } from "./CatalogItemCard"

/**
 * Columns follow the grid's own width, not the viewport's: beside the sidebar
 * a 1024 viewport leaves ~700px, where the old `lg:grid-cols-4` made 160px
 * tiles that the one footer button ("Open in Cinna Desktop") overflowed.
 */
export const CATALOG_GRID_CLASS =
  "grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-4 auto-rows-fr"

export function CatalogItemGrid({ items }: { items: CatalogItem[] }) {
  return (
    <div className={CATALOG_GRID_CLASS}>
      {items.map((item) => (
        <CatalogItemCard key={item.uuid} item={item} />
      ))}
    </div>
  )
}
