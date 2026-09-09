/**
 * SkillCatalogFilters — the subset filter for the skills catalog grid.
 *
 * Four states: `all` (default), `public`, `mine` (packages the caller
 * publishes), `installed` (packages already in one of their agents). Same
 * control as the bundle section's filter, `CatalogFilterMenu` — the two
 * sections share a page and would read as two products if their toolbars
 * differed.
 */
import { Download, Globe, ListFilter, User } from "lucide-react"

import {
  CatalogFilterMenu,
  type CatalogFilterOption,
} from "@/components/Catalog/CatalogFilterMenu"

export type SkillCatalogFilter = "all" | "public" | "mine" | "installed"

interface SkillCatalogFiltersProps {
  value: SkillCatalogFilter
  onChange: (next: SkillCatalogFilter) => void
}

const FILTERS: readonly CatalogFilterOption<SkillCatalogFilter>[] = [
  { value: "all", label: "All skills", icon: ListFilter },
  { value: "public", label: "Public", icon: Globe },
  { value: "mine", label: "Published by me", icon: User },
  { value: "installed", label: "Installed", icon: Download },
]

export function SkillCatalogFilters({
  value,
  onChange,
}: SkillCatalogFiltersProps) {
  return (
    <CatalogFilterMenu
      options={FILTERS}
      value={value}
      onChange={onChange}
      allValue="all"
    />
  )
}
