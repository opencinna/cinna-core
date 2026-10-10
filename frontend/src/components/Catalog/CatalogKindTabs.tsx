import { LayoutGrid, type LucideIcon } from "lucide-react"
import { type KeyboardEvent, useRef } from "react"

import { cn } from "@/lib/utils"
import {
  CATALOG_KINDS,
  type CatalogKind,
  type CatalogKindParam,
} from "@/utils/catalogItems"

const SEGMENTS: { value: CatalogKindParam; label: string; icon: LucideIcon }[] =
  [
    { value: "all", label: "All", icon: LayoutGrid },
    ...(["cloud", "desktop", "skill"] as CatalogKind[]).map((kind) => ({
      value: kind,
      label: CATALOG_KINDS[kind].segment,
      icon: CATALOG_KINDS[kind].icon,
    })),
  ]

interface CatalogKindTabsProps {
  value: CatalogKindParam
  onChange: (next: CatalogKindParam) => void
}

/**
 * Which kind of catalog item the page is showing — the successor of the old
 * Agents / Skills section tabs, on the same track with the same `bg-primary`
 * on-state (§2 Page toolbar). The kind changes what each tile's action is, so
 * it keeps the navigation vocabulary rather than joining the `Filters` menu.
 *
 * It no longer changes route, so it is a one-of-n choice and announces as one:
 * `radiogroup` + `radio`/`aria-checked`, with a roving tab stop and arrow keys
 * (§2 Choice-control ARIA — hand-rolled controls included).
 */
export function CatalogKindTabs({ value, onChange }: CatalogKindTabsProps) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0
    if (step === 0) return
    e.preventDefault()
    const next = (index + step + SEGMENTS.length) % SEGMENTS.length
    onChange(SEGMENTS[next].value)
    refs.current[next]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label="Kind of item"
      className="inline-flex flex-wrap items-center gap-1 rounded-lg border bg-muted/50 p-1"
    >
      {SEGMENTS.map((segment, index) => {
        const checked = segment.value === value
        const Icon = segment.icon
        return (
          // biome-ignore lint/a11y/useSemanticElements: a segmented control on the shared track; native radios cannot carry this skin, and the roving tab stop is implemented here.
          <button
            key={segment.value}
            ref={(el) => {
              refs.current[index] = el
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(segment.value)}
            onKeyDown={(e) => onKeyDown(e, index)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap",
              "transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              // The on-state is the brand colour: `bg-background` is darker than
              // the track on the dark skin, and `bg-accent` is the hover colour.
              checked
                ? "bg-primary text-primary-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {segment.label}
          </button>
        )
      })}
    </div>
  )
}
