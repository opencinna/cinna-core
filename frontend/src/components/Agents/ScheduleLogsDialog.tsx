import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  History,
  MinusCircle,
  Terminal,
} from "lucide-react"

import type { AgentScheduleLogPublic, AgentSchedulePublic } from "@/client"
import { AgentsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
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
import { Skeleton } from "@/components/ui/skeleton"
import { formatExecutedAt } from "./scheduleFormatting"

/**
 * The outcome of one run.
 *
 * The four tones are raw palette classes rather than semantic tokens: no
 * `--success` / `--warning` / `--neutral` token exists yet, and this is the
 * idiom nine files already share. Logged as the standing R14a follow-up
 * (widened from one token to a four-tone status set), not a new finding.
 */
function LogStatusBadge({ log }: { log: AgentScheduleLogPublic }) {
  if (log.status === "success") {
    const label =
      log.schedule_type === "script_trigger" ? "OK" : "Session created"
    return (
      <Badge
        variant="outline"
        className="text-xs text-green-600 border-green-300 bg-green-50"
      >
        <Check className="h-3 w-3 mr-1" />
        {label}
      </Badge>
    )
  }
  if (log.status === "session_triggered") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-amber-600 border-amber-300 bg-amber-50"
      >
        <Terminal className="h-3 w-3 mr-1" />
        Session triggered
      </Badge>
    )
  }
  if (log.status === "skipped") {
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
      className="text-xs text-red-600 border-red-300 bg-red-50"
    >
      <AlertCircle className="h-3 w-3 mr-1" />
      Error
    </Badge>
  )
}

/**
 * One execution, collapsed to when-and-how-it-went, expandable to the detail.
 *
 * Expand-in-place is allowed here because the content is read-only (§2 "Card
 * height"): nothing inside this row mutates anything, so growing it costs the
 * user nothing they did not ask for.
 *
 * The schedule's own type badge is deliberately absent: every log of one
 * schedule shares that schedule's type, so printing it per row is a constant,
 * and the type is already on the row that opened this dialog.
 */
function LogDetailRow({ log }: { log: AgentScheduleLogPublic }) {
  const [expanded, setExpanded] = useState(false)

  const hasDetail = Boolean(
    log.prompt_used ||
      log.command_executed ||
      log.command_output ||
      log.error_message,
  )

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs text-muted-foreground shrink-0">
            {formatExecutedAt(log.executed_at)}
          </span>
          <LogStatusBadge log={log} />
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
          {log.prompt_used && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Prompt used
              </div>
              <div className="bg-muted rounded p-2 whitespace-pre-wrap break-words">
                {log.prompt_used}
              </div>
            </div>
          )}
          {log.command_executed && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Command
              </div>
              <code className="bg-muted rounded px-2 py-1 block font-mono break-all">
                {log.command_executed}
              </code>
            </div>
          )}
          {log.command_exit_code !== null &&
            log.command_exit_code !== undefined && (
              <div className="flex items-center gap-2">
                <span className="font-medium text-muted-foreground">
                  Exit code:
                </span>
                <code
                  className={`px-2 py-0.5 rounded font-mono ${
                    log.command_exit_code === 0
                      ? "bg-green-100 text-green-700"
                      : "bg-red-100 text-red-700"
                  }`}
                >
                  {log.command_exit_code}
                </code>
              </div>
            )}
          {log.command_output && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Output
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.command_output}
              </pre>
            </div>
          )}
          {log.error_message && (
            <div>
              <div className="font-medium text-red-600 mb-1">Error</div>
              <div className="bg-red-50 border border-red-200 rounded p-2 text-red-700 break-words">
                {log.error_message}
              </div>
            </div>
          )}
          {log.session_id && (
            <div className="flex items-center gap-2">
              <span className="font-medium text-muted-foreground">
                Session:
              </span>
              <a
                href={`/session/${log.session_id}`}
                className="text-primary hover:underline flex items-center gap-1"
                target="_blank"
                rel="noopener noreferrer"
              >
                View session
                <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface ScheduleLogsDialogProps {
  agentId: string
  schedule: AgentSchedulePublic
  open: boolean
  onClose: () => void
}

/**
 * "Did this schedule actually run, and what happened?"
 *
 * A `Dialog` rather than a Sheet or a route, which is P5's accepted variant
 * for a read-only per-row detail list: capped at the server's last 50
 * records, scrolled inside `max-h-[60vh]`, and it opens nothing further.
 *
 * Owned by the row and mounted only while open, so the logs query belongs to
 * the schedule being looked at instead of to the card, and closing the dialog
 * stops it. Available in `readOnly` too — a bundle consumer who cannot edit a
 * publisher's schedule still needs to know whether it worked.
 */
export function ScheduleLogsDialog({
  agentId,
  schedule,
  open,
  onClose,
}: ScheduleLogsDialogProps) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["schedule-logs", schedule.id],
    queryFn: () =>
      AgentsService.listScheduleLogs({ id: agentId, scheduleId: schedule.id }),
    // Belt and braces: every caller already mounts this component only while
    // open, so this is always true. It stays so the query cannot start firing
    // if some future caller keeps the dialog resident.
    enabled: open,
  })

  const logs = data?.data ?? []

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 min-w-0">
            <History className="h-5 w-5 shrink-0" />
            <span className="truncate">Execution logs — {schedule.name}</span>
          </DialogTitle>
          <DialogDescription>
            Last 50 execution records for this schedule.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto space-y-2 pr-1">
          {isError && !data ? (
            // A failed read renders as a failure. Printing "no logs yet" here
            // would tell the user their schedule never ran.
            <QueryErrorAlert
              error={error}
              fallback="Couldn't load execution logs"
              onRetry={() => refetch()}
            />
          ) : isLoading ? (
            <>
              <Skeleton className="h-[38px] w-full rounded-md" />
              <Skeleton className="h-[38px] w-full rounded-md" />
              <Skeleton className="h-[38px] w-full rounded-md" />
            </>
          ) : logs.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No execution logs yet. Logs appear after the first scheduled
              execution.
            </p>
          ) : (
            logs.map((log) => <LogDetailRow key={log.id} log={log} />)
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
