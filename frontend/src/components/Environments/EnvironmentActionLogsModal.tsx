import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertCircle, Check, ChevronDown, ChevronUp, History, MinusCircle } from "lucide-react"
import { EnvironmentsService } from "@/client"
import type { AgentEnvActionLogPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

/**
 * Lazily-fetched modal listing the recent env-operation action log
 * (rebuild, setup, package install, credential/file sync, cron-skip) for an
 * environment. Mirrors the schedule execution-logs modal in `AgentSchedulesCard`:
 * a `max-w-2xl` Dialog with a scrollable body of expandable per-row entries.
 *
 * Powers the "Show details" affordance on `EnvironmentCriticalBadge`; the full,
 * untruncated `detail` (e.g. the raw uv resolver output) is what the owner needs
 * to fix their `workspace_requirements.txt`.
 */

// Renders a date as a coarse relative string (mirrors the schedule-logs helper).
function formatExecutedAt(isoDate: string): string {
  try {
    let utcDateString = isoDate
    if (
      !isoDate.endsWith("Z") &&
      !isoDate.includes("+") &&
      isoDate.includes("T")
    ) {
      utcDateString = isoDate + "Z"
    }
    const date = new Date(utcDateString)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffMinutes = Math.floor(diffMs / 60000)
    const diffHours = Math.floor(diffMinutes / 60)
    const diffDays = Math.floor(diffHours / 24)

    if (diffMinutes < 1) return "Just now"
    if (diffMinutes < 60) return `${diffMinutes}m ago`
    if (diffHours < 24) return `${diffHours}h ago`
    if (diffDays < 7) return `${diffDays}d ago`

    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return isoDate
  }
}

// error → red, skipped → neutral/gray, success (and anything else) → green.
function ActionLogStatusBadge({ status }: { status: string }) {
  if (status === "error") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-red-600 border-red-300 bg-red-50"
      >
        <AlertCircle className="h-3 w-3 mr-1" />
        Error
      </Badge>
    )
  }
  if (status === "skipped") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-gray-600 border-gray-300 bg-gray-50"
      >
        <MinusCircle className="h-3 w-3 mr-1" />
        Skipped
      </Badge>
    )
  }
  return (
    <Badge
      variant="outline"
      className="text-xs text-green-600 border-green-300 bg-green-50"
    >
      <Check className="h-3 w-3 mr-1" />
      Success
    </Badge>
  )
}

function ActionLogRow({ log }: { log: AgentEnvActionLogPublic }) {
  const [expanded, setExpanded] = useState(false)
  const hasDetail = !!log.summary || !!log.detail
  const isError = log.status === "error"

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30">
        <div className="flex items-center gap-2 min-w-0">
          <Badge variant="outline" className="text-xs shrink-0 font-mono">
            {log.action}
          </Badge>
          <span className="text-xs text-muted-foreground shrink-0">
            {formatExecutedAt(log.executed_at)}
          </span>
          <ActionLogStatusBadge status={log.status} />
        </div>
        {hasDetail && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs shrink-0"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <ChevronUp className="h-3 w-3" />
            ) : (
              <ChevronDown className="h-3 w-3" />
            )}
            {expanded ? "Hide" : "View"}
          </Button>
        )}
      </div>

      {expanded && (
        <div className="px-3 py-2 space-y-2 bg-background border-t text-xs">
          {log.summary && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Summary
              </div>
              <div className="bg-muted rounded p-2 whitespace-pre-wrap break-words">
                {log.summary}
              </div>
            </div>
          )}
          {log.detail && (
            <div>
              <div
                className={
                  isError
                    ? "font-medium text-red-600 mb-1"
                    : "font-medium text-muted-foreground mb-1"
                }
              >
                Detail
              </div>
              <pre
                className={
                  isError
                    ? "bg-red-50 border border-red-200 rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words text-red-700"
                    : "bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words"
                }
              >
                {log.detail}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface EnvironmentActionLogsModalProps {
  environmentId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EnvironmentActionLogsModal({
  environmentId,
  open,
  onOpenChange,
}: EnvironmentActionLogsModalProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["env-action-logs", environmentId],
    queryFn: () =>
      EnvironmentsService.getEnvironmentActionLogs({ environmentId }),
    enabled: open && !!environmentId,
  })

  const logs = data?.data ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <History className="h-5 w-5" />
            Environment details
          </DialogTitle>
          <DialogDescription>
            Recent setup and provisioning records for this environment. Error
            entries include the full output so you can resolve the issue.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-y-auto space-y-2 pr-1">
          {isLoading ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              Loading activity...
            </p>
          ) : isError ? (
            <p className="text-sm text-destructive text-center py-8">
              Couldn&apos;t load activity. Please try again.
            </p>
          ) : logs.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No activity recorded yet.
            </p>
          ) : (
            logs.map((log) => <ActionLogRow key={log.id} log={log} />)
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
