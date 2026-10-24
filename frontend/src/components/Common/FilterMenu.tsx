import type { LucideIcon } from "lucide-react"
import { SlidersHorizontal } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

export interface FilterOption<T extends string> {
  value: T
  label: string
  icon: LucideIcon
}

interface FilterMenuProps<T extends string> {
  options: readonly FilterOption<T>[]
  value: T
  onChange: (next: T) => void
  /** The option that means "everything" — the trigger stays plain on it. */
  allValue: T
}

/**
 * The subset filter for a list or grid, as **one** button that opens a menu
 * (§2 Page toolbars). Moved here from `Catalog/` at its marketplace-entries
 * consumer; the history below is the catalog grid's.
 *
 * It used to be a row of four pills directly under the section tabs — and
 * those tabs are also a row of pills, built to the same skeleton on purpose. So
 * the page opened with eight identical controls in two rows, half of them
 * navigation and half of them a filter, with nothing but their labels saying
 * which was which. A filter is a refinement of what is already on screen; it is
 * worth one control, and the section switch beside it gets to be the only thing
 * on the toolbar that looks like a choice of place.
 *
 * The active subset rides on the trigger, so a narrowed grid always says why it
 * is short without the menu being open. Callers own only their option list.
 */
export function FilterMenu<T extends string>({
  options,
  value,
  onChange,
  allValue,
}: FilterMenuProps<T>) {
  const active = options.find((option) => option.value === value)
  const isNarrowed = value !== allValue

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <SlidersHorizontal className="h-3.5 w-3.5" />
          Filters
          {/* Not a `Badge`: a count badge is a number, and this is the name of
              the one active subset. A separator plus the label reads as "the
              button, narrowed to this" rather than as a second control. */}
          {isNarrowed && active && (
            <>
              <span aria-hidden className="text-muted-foreground">
                ·
              </span>
              <span className="font-normal">{active.label}</span>
            </>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel>Show</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={value}
          // Radix hands back a plain string; the group's own options are the
          // only values it can produce.
          onValueChange={(next) => onChange(next as T)}
        >
          {options.map((option) => {
            const Icon = option.icon
            return (
              <DropdownMenuRadioItem key={option.value} value={option.value}>
                <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                {option.label}
              </DropdownMenuRadioItem>
            )
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
