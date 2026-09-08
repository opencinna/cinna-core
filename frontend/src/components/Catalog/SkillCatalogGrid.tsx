/**
 * SkillCatalogGrid — responsive grid of `SkillCatalogCard`s.
 *
 * Same grid classes as `CatalogGrid`, deliberately: the two catalog sections
 * are one page with two tabs, so a card must not change size when the tab does.
 */
import type { SkillPackageEntry } from "@/client"

import { SkillCatalogCard } from "./SkillCatalogCard"

interface SkillCatalogGridProps {
  entries: SkillPackageEntry[]
}

export function SkillCatalogGrid({ entries }: SkillCatalogGridProps) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
      {entries.map((entry) => (
        <SkillCatalogCard key={entry.id} entry={entry} />
      ))}
    </div>
  )
}
