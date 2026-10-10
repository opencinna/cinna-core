import { Loader2 } from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/**
 * A write in flight, shown in the `⋯` menu's slot (§2 Pending on rows): a
 * spinner the size of the trigger, with the wait named in a tooltip and for
 * screen readers.
 */
export function PendingSlot({ label }: { label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex h-7 w-7 items-center justify-center">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          <span className="sr-only">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}
