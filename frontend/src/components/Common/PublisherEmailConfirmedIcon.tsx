/**
 * PublisherEmailConfirmedIcon — green check / amber warning shown beside a
 * bundle publisher's email on catalog and install cards.
 *
 * Renders nothing when there is no publisher email to qualify.
 */
import { BadgeCheck, AlertTriangle } from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface PublisherEmailConfirmedIconProps {
  confirmed: boolean
  /** Only render when there is actually a publisher email shown. */
  hasEmail: boolean
}

export function PublisherEmailConfirmedIcon({
  confirmed,
  hasEmail,
}: PublisherEmailConfirmedIconProps) {
  if (!hasEmail) return null

  const label = confirmed
    ? "Publisher email confirmed"
    : "Publisher email not confirmed"

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0" aria-label={label}>
            {confirmed ? (
              <BadgeCheck className="h-3.5 w-3.5 text-green-600" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
            )}
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
