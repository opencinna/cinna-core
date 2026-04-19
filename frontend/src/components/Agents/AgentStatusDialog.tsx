import { cn } from "@/lib/utils"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { RefreshCw, Copy } from "lucide-react"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { useAgentStatus, isRecentTransition, severityDotClass, severityLabel } from "@/hooks/useAgentStatus"
import useCustomToast from "@/hooks/useCustomToast"

interface AgentStatusDialogProps {
  agentId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Full-detail modal for an agent's self-reported status.
 *
 * Header strip:
 *   - Severity colored dot + label + summary
 *   - reported_at relative + absolute, with note when inferred from file mtime
 *   - "Changed from prev_severity" line when a transition occurred within 1 h
 *
 * Body: full markdown via MarkdownRenderer
 *
 * Footer: Refresh (force_refresh=true, 429 swallowed), Copy raw, Close
 *
 * Empty state when agent has never published STATUS.md.
 */
export function AgentStatusDialog({ agentId, open, onOpenChange }: AgentStatusDialogProps) {
  const { status, isLoading, forceRefresh, isRefreshing } = useAgentStatus(
    agentId,
    open
  )
  const { showSuccessToast } = useCustomToast()

  const hasNeverPublished =
    !isLoading && (!status || (status.severity == null && status.raw == null))

  const hasTransition = status ? isRecentTransition(status) : false

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

  const handleCopy = () => {
    if (!status?.raw) return
    navigator.clipboard.writeText(status.raw).then(() => {
      showSuccessToast("Copied to clipboard")
    })
  }

  // `body` is the raw file with the YAML frontmatter block stripped server-side,
  // so the header strip's severity/summary/timestamp are not duplicated here.
  // `raw` is still used for the Copy button so users get the verbatim file.
  const bodyContent = status?.body ?? status?.raw ?? null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {status && (
              <span
                className={cn(
                  "inline-block h-3 w-3 rounded-full shrink-0",
                  severityDotClass(status.severity)
                )}
              />
            )}
            Agent Status
          </DialogTitle>
        </DialogHeader>

        {/* Header strip */}
        {!isLoading && status && !hasNeverPublished && (
          <div className="space-y-1 px-1 text-sm border-b pb-3">
            {/* Summary */}
            {status.summary && (
              <p className="font-medium text-foreground">{status.summary}</p>
            )}

            {/* Severity label */}
            <p className="text-xs text-muted-foreground">
              Severity:{" "}
              <span className="font-medium text-foreground">
                {severityLabel(status.severity)}
              </span>
            </p>

            {/* reported_at */}
            {status.reported_at && (
              <p className="text-xs text-muted-foreground">
                Reported:{" "}
                <RelativeTime timestamp={status.reported_at} showTooltip />{" "}
                <span className="text-muted-foreground/70">
                  ({getAbsoluteTimestamp(status.reported_at)})
                </span>
              </p>
            )}

            {/* Inferred mtime notice */}
            {status.reported_at_source === "file_mtime" && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                Timestamp inferred from file modification time — no explicit
                timestamp in STATUS.md frontmatter.
              </p>
            )}

            {/* fetched_at */}
            {status.fetched_at && (
              <p className="text-xs text-muted-foreground">
                Last fetched:{" "}
                <RelativeTime timestamp={status.fetched_at} showTooltip />
              </p>
            )}

            {/* Transition line */}
            {hasTransition && status.prev_severity && status.severity_changed_at && (
              <p className="text-xs text-muted-foreground flex items-center gap-1">
                Changed from{" "}
                <span className="flex items-center gap-1 font-medium text-foreground">
                  <span
                    className={cn(
                      "inline-block h-2 w-2 rounded-full",
                      severityDotClass(status.prev_severity)
                    )}
                  />
                  {severityLabel(status.prev_severity)}
                </span>{" "}
                <RelativeTime timestamp={status.severity_changed_at} showTooltip />
              </p>
            )}

          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto min-h-0">
          {isLoading && (
            <div className="space-y-2 p-4">
              <div className="h-4 bg-muted rounded animate-pulse w-3/4" />
              <div className="h-4 bg-muted rounded animate-pulse w-full" />
              <div className="h-4 bg-muted rounded animate-pulse w-1/2" />
            </div>
          )}

          {!isLoading && hasNeverPublished && (
            <div className="flex flex-col items-center justify-center h-32 text-center px-4">
              <p className="text-sm text-muted-foreground">
                Agent has not published a STATUS.md yet.
              </p>
            </div>
          )}

          {!isLoading && status && !hasNeverPublished && (
            <div className="px-1 py-2">
              {bodyContent && bodyContent.trim() ? (
                <MarkdownRenderer
                  content={bodyContent}
                  className="prose prose-sm dark:prose-invert max-w-none text-sm"
                />
              ) : (
                <p className="text-sm text-muted-foreground italic">
                  No additional details.
                </p>
              )}
            </div>
          )}
        </div>

        <DialogFooter className="flex flex-row items-center gap-2 pt-2 border-t">
          <Button
            variant="outline"
            size="sm"
            onClick={forceRefresh}
            disabled={isRefreshing || hasNeverPublished}
            className="gap-1.5"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isRefreshing && "animate-spin")} />
            Refresh
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleCopy}
            disabled={!status?.raw}
            className="gap-1.5"
          >
            <Copy className="h-3.5 w-3.5" />
            Copy
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            className="ml-auto"
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
