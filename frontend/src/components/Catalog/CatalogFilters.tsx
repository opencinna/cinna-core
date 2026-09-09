/**
 * CatalogFilters — the subset filter for the bundle catalog grid.
 *
 * Four states: `all` (default), `public`, `shared` (only granted), `installed`
 * (only bundles the user has already installed). The control is
 * `CatalogFilterMenu`; this file is the option list.
 */
import { Download, Globe, ListFilter, Users } from "lucide-react"

import {
  CatalogFilterMenu,
  type CatalogFilterOption,
} from "@/components/Catalog/CatalogFilterMenu"

export type CatalogFilter = "all" | "public" | "shared" | "installed"

interface CatalogFiltersProps {
  value: CatalogFilter
  onChange: (next: CatalogFilter) => void
}

const FILTERS: readonly CatalogFilterOption<CatalogFilter>[] = [
  { value: "all", label: "All bundles", icon: ListFilter },
  { value: "public", label: "Public", icon: Globe },
  { value: "shared", label: "Shared with me", icon: Users },
  { value: "installed", label: "Installed", icon: Download },
]

export function CatalogFilters({ value, onChange }: CatalogFiltersProps) {
  return (
    <CatalogFilterMenu
      options={FILTERS}
      value={value}
      onChange={onChange}
      allValue="all"
    />
  )
}
