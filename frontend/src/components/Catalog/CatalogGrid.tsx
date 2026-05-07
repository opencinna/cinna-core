/**
 * CatalogGrid — responsive grid of ``CatalogCard``s.
 */
import type { CatalogEntryPublic } from "@/client"

import { CatalogCard } from "./CatalogCard"

interface CatalogGridProps {
  entries: CatalogEntryPublic[]
}

export function CatalogGrid({ entries }: CatalogGridProps) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
      {entries.map((entry) => (
        <CatalogCard key={entry.bundle_id} entry={entry} />
      ))}
    </div>
  )
}
