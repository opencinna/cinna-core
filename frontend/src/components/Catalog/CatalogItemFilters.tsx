/**
 * CatalogItemFilters — the subset filter for the unified catalog grid.
 *
 * One option list for every kind (it replaces the bundle and skill lists that
 * sat beside two section tabs). The control is `CatalogFilterMenu`; this file
 * is the option list. "Mine" is what the caller can share changes to or
 * maintains — the server's `access`, not a publisher id compared in the client.
 */
import { Download, Globe, ListFilter, User, Users } from "lucide-react"

import {
  CatalogFilterMenu,
  type CatalogFilterOption,
} from "@/components/Catalog/CatalogFilterMenu"
import type { CatalogFilter } from "@/utils/catalogItems"

interface CatalogItemFiltersProps {
  value: CatalogFilter
  onChange: (next: CatalogFilter) => void
}

const FILTERS: readonly CatalogFilterOption<CatalogFilter>[] = [
  { value: "all", label: "All", icon: ListFilter },
  { value: "public", label: "Public", icon: Globe },
  { value: "shared", label: "Shared with me", icon: Users },
  { value: "installed", label: "Installed", icon: Download },
  { value: "mine", label: "Mine", icon: User },
]

export function CatalogItemFilters({
  value,
  onChange,
}: CatalogItemFiltersProps) {
  return (
    <CatalogFilterMenu
      options={FILTERS}
      value={value}
      onChange={onChange}
      allValue="all"
    />
  )
}
