/**
 * SkillCatalogFilters — segmented filter pills for the skills catalog grid.
 *
 * Four states: `all` (default), `public`, `mine` (packages the caller
 * publishes), `installed` (packages already in one of their agents). Built to
 * `CatalogFilters`'s skeleton on purpose — the two catalog sections share a
 * page and would read as two products if their toolbars differed.
 */
import { Download, Globe, ListFilter, User } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type SkillCatalogFilter = "all" | "public" | "mine" | "installed"

interface SkillCatalogFiltersProps {
  value: SkillCatalogFilter
  onChange: (next: SkillCatalogFilter) => void
}

const FILTERS: {
  value: SkillCatalogFilter
  label: string
  icon: ReactNode
}[] = [
  { value: "all", label: "All", icon: <ListFilter className="h-3.5 w-3.5" /> },
  { value: "public", label: "Public", icon: <Globe className="h-3.5 w-3.5" /> },
  { value: "mine", label: "Mine", icon: <User className="h-3.5 w-3.5" /> },
  {
    value: "installed",
    label: "Installed",
    icon: <Download className="h-3.5 w-3.5" />,
  },
]

export function SkillCatalogFilters({
  value,
  onChange,
}: SkillCatalogFiltersProps) {
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
