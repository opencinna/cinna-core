import type { LucideIcon } from "lucide-react"
import { SlidersHorizontal } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

export interface FilterOption<T extends string> {
  value: T
  label: string
  icon: LucideIcon
}

/**
 * A second, independent subset in the same menu (a list narrowed both by state
 * and by type). Still one control: its own labelled radio group under a
 * separator, and its active subset joins the first on the trigger.
 */
export interface FilterGroup<U extends string> {
  label: string
  options: readonly FilterOption<U>[]
  value: U
  onChange: (next: U) => void
  allValue: U
}

interface FilterMenuProps<T extends string, U extends string> {
  options: readonly FilterOption<T>[]
  value: T
  onChange: (next: T) => void
  /** The option that means "everything" — the trigger stays plain on it. */
  allValue: T
  secondary?: FilterGroup<U>
}

function RadioOptions<V extends string>({
  options,
  value,
  onChange,
}: {
  options: readonly FilterOption<V>[]
  value: V
  onChange: (next: V) => void
}) {
  return (
    <DropdownMenuRadioGroup
      value={value}
      // Radix hands back a plain string; the group's own options are the
      // only values it can produce.
      onValueChange={(next) => onChange(next as V)}
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
  )
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
export function FilterMenu<T extends string, U extends string = string>({
  options,
  value,
  onChange,
  allValue,
  secondary,
}: FilterMenuProps<T, U>) {
  const activeLabels = [
    value !== allValue &&
      options.find((option) => option.value === value)?.label,
    secondary &&
      secondary.value !== secondary.allValue &&
      secondary.options.find((option) => option.value === secondary.value)
        ?.label,
  ].filter((label): label is string => !!label)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <SlidersHorizontal className="h-3.5 w-3.5" />
          Filters
          {/* Not a `Badge`: a count badge is a number, and this is the name of
              the active subset(s). A separator plus the label reads as "the
              button, narrowed to this" rather than as a second control. */}
          {activeLabels.map((label) => (
            <span key={label} className="contents">
              <span aria-hidden className="text-muted-foreground">
                ·
              </span>
              <span className="font-normal">{label}</span>
            </span>
          ))}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel>Show</DropdownMenuLabel>
        <RadioOptions options={options} value={value} onChange={onChange} />
        {secondary && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>{secondary.label}</DropdownMenuLabel>
            <RadioOptions
              options={secondary.options}
              value={secondary.value}
              onChange={secondary.onChange}
            />
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
