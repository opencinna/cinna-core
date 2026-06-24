/**
 * CredentialFilters — segmented filter pills for the Credentials grid.
 *
 * Three states: ``mine`` (default, owned non-automatic + direct-shared),
 * ``automatic`` (owned agent_api / mcp_provider connections), ``bundle``
 * (credentials shared in via a bundle install). Mirrors CatalogFilters.
 */
import { Key, Link2, Package } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type CredentialFilter = "mine" | "automatic" | "bundle"

interface CredentialFiltersProps {
  value: CredentialFilter
  onChange: (next: CredentialFilter) => void
}

const FILTERS: { value: CredentialFilter; label: string; icon: ReactNode }[] = [
  { value: "mine", label: "My Credentials", icon: <Key className="h-3.5 w-3.5" /> },
  { value: "automatic", label: "Automatic Credentials", icon: <Link2 className="h-3.5 w-3.5" /> },
  { value: "bundle", label: "Bundle Credentials", icon: <Package className="h-3.5 w-3.5" /> },
]

export function CredentialFilters({ value, onChange }: CredentialFiltersProps) {
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
