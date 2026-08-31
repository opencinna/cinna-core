import { useEffect, useRef, useState } from "react"
import type { AdminAgentEnvironmentsPublic } from "@/client"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Search } from "lucide-react"

const ALL_STATUSES = [
  "stopped",
  "creating",
  "building",
  "initializing",
  "starting",
  "running",
  "rebuilding",
  "suspended",
  "activating",
  "error",
  "deprecated",
]

export interface AdminEnvFilters {
  template: string | null
  status: string | null
  isStale: boolean | null
  inUse: boolean | null
  /** Consumer installs running an older revision than their bundle's latest. */
  updateAvailable: boolean | null
  search: string
}

interface AdminEnvFiltersBarProps {
  data: AdminAgentEnvironmentsPublic | undefined
  filters: AdminEnvFilters
  onFiltersChange: (next: AdminEnvFilters) => void
}

export function AdminEnvFiltersBar({
  data,
  filters,
  onFiltersChange,
}: AdminEnvFiltersBarProps) {
  const [searchInput, setSearchInput] = useState(filters.search)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounce search input
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      if (searchInput !== filters.search) {
        onFiltersChange({ ...filters, search: searchInput })
      }
    }, 350)
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [searchInput]) // eslint-disable-line react-hooks/exhaustive-deps

  const templateNames = data?.templates.map((t) => t.env_name) ?? []

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Template filter */}
      <Select
        value={filters.template ?? "all"}
        onValueChange={(v) =>
          onFiltersChange({ ...filters, template: v === "all" ? null : v })
        }
      >
        <SelectTrigger className="h-8 w-44 text-xs">
          <SelectValue placeholder="All templates" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All templates</SelectItem>
          {templateNames.map((name) => (
            <SelectItem key={name} value={name}>
              {name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {/* Status filter */}
      <Select
        value={filters.status ?? "all"}
        onValueChange={(v) =>
          onFiltersChange({ ...filters, status: v === "all" ? null : v })
        }
      >
        <SelectTrigger className="h-8 w-36 text-xs">
          <SelectValue placeholder="All statuses" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All statuses</SelectItem>
          {ALL_STATUSES.map((s) => (
            <SelectItem key={s} value={s}>
              {s}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {/* Only stale toggle */}
      <Button
        size="sm"
        variant={filters.isStale === true ? "default" : "outline"}
        onClick={() =>
          onFiltersChange({
            ...filters,
            isStale: filters.isStale === true ? null : true,
          })
        }
        className={
          filters.isStale === true
            ? "h-8 px-3 text-xs bg-orange-100 text-orange-800 border-orange-300 hover:bg-orange-200 dark:bg-orange-900 dark:text-orange-200 dark:border-orange-700"
            : "h-8 px-3 text-xs"
        }
        aria-pressed={filters.isStale === true}
      >
        Only stale
      </Button>

      {/* Only in-use toggle */}
      <Button
        size="sm"
        variant={filters.inUse === true ? "default" : "outline"}
        onClick={() =>
          onFiltersChange({
            ...filters,
            inUse: filters.inUse === true ? null : true,
          })
        }
        className={
          filters.inUse === true
            ? "h-8 px-3 text-xs bg-blue-100 text-blue-800 border-blue-300 hover:bg-blue-200 dark:bg-blue-900 dark:text-blue-200 dark:border-blue-700"
            : "h-8 px-3 text-xs"
        }
        aria-pressed={filters.inUse === true}
      >
        Only in use
      </Button>

      {/* Only bundle-update-available toggle — amber, matching the Bundle
          column's update badge and deliberately distinct from the orange
          image-staleness language above. */}
      <Button
        size="sm"
        variant={filters.updateAvailable === true ? "default" : "outline"}
        onClick={() =>
          onFiltersChange({
            ...filters,
            updateAvailable: filters.updateAvailable === true ? null : true,
          })
        }
        className={
          filters.updateAvailable === true
            ? "h-8 px-3 text-xs bg-amber-100 text-amber-800 border-amber-300 hover:bg-amber-200 dark:bg-amber-900 dark:text-amber-200 dark:border-amber-700"
            : "h-8 px-3 text-xs"
        }
        aria-pressed={filters.updateAvailable === true}
      >
        Bundle update available
      </Button>

      {/* Text search */}
      <div className="relative">
        <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
        <Input
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search agent, owner, instance..."
          className="h-8 pl-7 text-xs w-56"
        />
      </div>
    </div>
  )
}
