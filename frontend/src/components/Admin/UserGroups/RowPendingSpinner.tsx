import { Loader2 } from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/**
 * A write in flight on a table row, in the slot of the row's action (§2
 * Pending on rows): a spinner whose tooltip names the wait, plus an `sr-only`
 * copy of it. Same markup as `credentialColumns`' deleting state.
 */
export function RowPendingSpinner({
  label,
  srLabel,
}: {
  /** The mutation's name: "Removing…", "Revoking…". */
  label: string
  /** The sentence a screen reader hears, naming the row. */
  srLabel: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="flex h-7 w-7 items-center justify-center"
          aria-live="polite"
        >
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          <span className="sr-only">{srLabel}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}
