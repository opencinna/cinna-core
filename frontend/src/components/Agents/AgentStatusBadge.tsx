import { useState } from "react"
import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { useAgentStatus, isRecentTransition, severityDotClass, severityLabel } from "@/hooks/useAgentStatus"
import { AgentStatusDialog } from "./AgentStatusDialog"

interface AgentStatusBadgeProps {
  agentId: string
  className?: string
}

/**
 * Compact badge displaying an agent's self-reported status.
 *
 * - Colored dot + truncated summary + relative "reported_at" timestamp
 * - Tooltip with absolute local-tz timestamp
 * - "prev → current" chip when transition happened ≤ 1 h ago
 * - Dimmed + "Outdated" pill when is_stale = true
 * - Hidden when both severity and raw are null (agent never published STATUS.md)
 * - Click opens AgentStatusDialog
 */
export function AgentStatusBadge({ agentId, className }: AgentStatusBadgeProps) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const { status, isLoading } = useAgentStatus(agentId, dialogOpen)

  // Loading skeleton
  if (isLoading) {
    return (
      <div className={cn("flex items-center gap-1.5 px-2 py-1 rounded-md animate-pulse", className)}>
        <div className="h-2 w-2 rounded-full bg-muted-foreground/30 shrink-0" />
        <div className="h-3 w-20 bg-muted-foreground/20 rounded" />
      </div>
    )
  }

  // Hide entirely when agent has never published STATUS.md
  if (!status || (status.severity == null && status.raw == null)) {
    return null
  }

  const isStale = status.is_stale ?? false
  const hasTransition = isRecentTransition(status)

  // Build absolute timestamp string for tooltip
  const getAbsoluteTimestamp = (ts: string | null | undefined): string => {
    if (!ts) return ""
    try {
      const date = new Date(ts.endsWith("Z") ? ts : `${ts}Z`)
      if (isNaN(date.getTime())) return ts
      return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      })
    } catch {
      return ts
    }
  }

  const absoluteTime = getAbsoluteTimestamp(status.reported_at)

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label="Show agent status"
              onClick={() => setDialogOpen(true)}
              className={cn(
                "flex items-center gap-1.5 px-2 py-1 rounded-md text-xs",
                "hover:bg-accent transition-colors text-left",
                isStale && "opacity-60",
                className
              )}
            >
              {/* Severity dot */}
              <span
                className={cn(
                  "h-2 w-2 rounded-full shrink-0",
                  severityDotClass(status.severity)
                )}
              />

              {/* Summary text — truncated */}
              <span
                className="max-w-[240px] truncate text-foreground/80"
                aria-live="polite"
              >
                {status.summary ?? severityLabel(status.severity)}
              </span>

              {/* Stale indicator */}
              {isStale && (
                <span className="shrink-0 px-1 py-0 rounded text-[10px] font-medium bg-muted text-muted-foreground border border-border">
                  Outdated
                </span>
              )}

              {/* Recent transition chip: prev → current */}
              {hasTransition && status.prev_severity && (
                <span className="shrink-0 flex items-center gap-0.5 px-1 py-0 rounded text-[10px] font-medium bg-muted text-muted-foreground border border-border">
                  <span
                    className={cn(
                      "inline-block h-1.5 w-1.5 rounded-full",
                      severityDotClass(status.prev_severity)
                    )}
                  />
                  <span>→</span>
                  <span
                    className={cn(
                      "inline-block h-1.5 w-1.5 rounded-full",
                      severityDotClass(status.severity)
                    )}
                  />
                </span>
              )}

              {/* Relative time */}
              {status.reported_at && (
                <span className="shrink-0 text-muted-foreground">
                  <RelativeTime timestamp={status.reported_at} />
                </span>
              )}
            </button>
          </TooltipTrigger>

          {absoluteTime && (
            <TooltipContent side="bottom" className="text-xs">
              {absoluteTime}
            </TooltipContent>
          )}
        </Tooltip>
      </TooltipProvider>

      <AgentStatusDialog
        agentId={agentId}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
      />
    </>
  )
}
