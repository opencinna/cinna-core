import { EllipsisVertical } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface RowActionsMenuProps {
  /**
   * Names the entity: "the schedule Nightly sync". Read by screen readers as
   * "Actions for …", so a list of rows does not become a list of identical
   * "More actions" buttons.
   */
  label: string
  /** Menu items. Destructive ones last, after a `DropdownMenuSeparator`. */
  children: ReactNode
  /** A write on this row is in flight; the trigger freezes. */
  disabled?: boolean
}

/**
 * The `⋯` overflow menu that ends every list row (guidelines §2 "Row actions").
 *
 * Extracted from the identical block in `HandoverRow`, `ScheduleRow`,
 * `AppSessionRow` and the rows this refactor rebuilt: same button, same size,
 * same tooltip, same `align="end"`. What it makes structural rather than
 * per-author is the pair of things hand-written triggers kept dropping — the
 * tooltip on an icon-only button, and an `aria-label` that names the row.
 *
 * Items stay with their feature: a menu item usually sets state that the row
 * owns (a dialog, a confirm), because a `DropdownMenuItem` unmounts on select
 * and would take a nested trigger's pending state with it.
 */
export function RowActionsMenu({
  label,
  children,
  disabled,
}: RowActionsMenuProps) {
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              disabled={disabled}
              aria-label={`Actions for ${label}`}
            >
              <EllipsisVertical className="h-3.5 w-3.5" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          More actions
        </TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end">{children}</DropdownMenuContent>
    </DropdownMenu>
  )
}
