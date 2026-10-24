import type { RowStatusTone } from "@/components/Common/ListRow"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/** Same four tones, same colours, as `ListRow`'s leading dot. */
const TONE_CLASS: Record<RowStatusTone, string> = {
  on: "bg-success",
  off: "bg-muted-foreground/40",
  warning: "bg-warning",
  error: "bg-destructive",
}

interface StatusDotProps {
  tone: RowStatusTone
  /** The state in words, always visible beside the dot. */
  label: string
  /**
   * Why the row is in this state. Rendered as the house's "more on hover"
   * affordance — a dotted underline under the label and a tooltip — and as
   * `sr-only` text, so a reader that never gets the tooltip still gets it.
   */
  reason?: string | null
  className?: string
}

/**
 * A state as a tone dot plus a label: the form §5 admits in a `DataTable`
 * cell and a fact grid, where a state and the reason for it are two unequal
 * facts and two badges would flatten them.
 *
 * Extracted from the marketplace entries table's `SupportedCell` and the
 * plugin detail route's Installation fact, which drew the same dot by hand.
 */
export function StatusDot({ tone, label, reason, className }: StatusDotProps) {
  const dot = (
    <span className={cn("size-2 shrink-0 rounded-full", TONE_CLASS[tone])} />
  )

  if (!reason) {
    return (
      <span className={cn("flex w-fit items-center gap-2", className)}>
        {dot}
        <span className="text-sm">{label}</span>
      </span>
    )
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("flex w-fit items-center gap-2", className)}>
          {dot}
          <span className="cursor-default text-sm underline decoration-dotted underline-offset-2">
            {label}
          </span>
          <span className="sr-only">{reason}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        {reason}
      </TooltipContent>
    </Tooltip>
  )
}
