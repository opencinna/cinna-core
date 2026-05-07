/**
 * CatalogFilters — segmented filter pills for the Catalog grid.
 *
 * Three states: ``all`` (default), ``public``, ``shared`` (only granted),
 * ``installed`` (only bundles the user has already installed).
 */
import { Globe, ListFilter, Users, Download } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type CatalogFilter = "all" | "public" | "shared" | "installed"

interface CatalogFiltersProps {
  value: CatalogFilter
  onChange: (next: CatalogFilter) => void
}

const FILTERS: { value: CatalogFilter; label: string; icon: ReactNode }[] = [
  { value: "all", label: "All", icon: <ListFilter className="h-3.5 w-3.5" /> },
  { value: "public", label: "Public", icon: <Globe className="h-3.5 w-3.5" /> },
  { value: "shared", label: "Shared with me", icon: <Users className="h-3.5 w-3.5" /> },
  { value: "installed", label: "Installed", icon: <Download className="h-3.5 w-3.5" /> },
]

export function CatalogFilters({ value, onChange }: CatalogFiltersProps) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {FILTERS.map((f) => {
        const active = value === f.value
        return (
          <Button
            key={f.value}
            variant={active ? "default" : "outline"}
            size="sm"
            onClick={() => onChange(f.value)}
            className={cn("gap-1.5", !active && "text-muted-foreground")}
          >
            {f.icon}
            {f.label}
          </Button>
        )
      })}
    </div>
  )
}
