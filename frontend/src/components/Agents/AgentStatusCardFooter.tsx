import { useState, type MouseEvent } from "react"
import { cn } from "@/lib/utils"
import type { AgentStatusPublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { severityDotClass, severityLabel } from "@/hooks/useAgentStatus"
import { AgentStatusDialog } from "./AgentStatusDialog"

interface AgentStatusCardFooterProps {
  agentId: string
  status: AgentStatusPublic
  className?: string
}

/**
 * Compact one-line status footer for use inside agent list cards.
 *
 * - Renders nothing when the agent has never published a status.
 * - Click opens AgentStatusDialog. Stops propagation so the wrapping card
 *   Link does not navigate.
 */
export function AgentStatusCardFooter({
  agentId,
  status,
  className,
}: AgentStatusCardFooterProps) {
  const [dialogOpen, setDialogOpen] = useState(false)

  const hasStatus = status.severity != null || status.raw != null
  if (!hasStatus) return null

  const openDialog = (e: MouseEvent<HTMLButtonElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setDialogOpen(true)
  }

  return (
    <>
      <button
        type="button"
        aria-label="Show agent status"
        onClick={openDialog}
        className={cn(
          "w-full flex items-center gap-1.5 px-3 py-2 border-t text-xs text-left",
          "hover:bg-accent transition-colors",
          className,
        )}
      >
        <span
          className={cn(
            "h-2 w-2 rounded-full shrink-0",
            severityDotClass(status.severity),
          )}
        />
        <span className="flex-1 min-w-0 truncate text-foreground/80">
          {status.summary ?? severityLabel(status.severity)}
        </span>
        {status.reported_at && (
          <span className="shrink-0 text-muted-foreground">
            <RelativeTime timestamp={status.reported_at} showTooltip />
          </span>
        )}
      </button>
      <AgentStatusDialog
        agentId={agentId}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
      />
    </>
  )
}
