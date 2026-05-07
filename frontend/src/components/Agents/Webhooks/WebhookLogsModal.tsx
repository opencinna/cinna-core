import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  History,
  Play,
  X,
  Zap,
} from "lucide-react"

import type { AgentWebhookLogPublic, AgentWebhookPublic } from "@/client"
import { AgentWebhooksService } from "@/client"
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

interface WebhookLogsModalProps {
  agentId: string
  webhook: AgentWebhookPublic | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

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

function LogStatusBadge({ status }: { status: string }) {
  if (status === "session_started") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-green-600 border-green-300 bg-green-50"
      >
        <Play className="h-3 w-3 mr-1" />
        Session started
      </Badge>
    )
  }
  if (status === "success") {
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
  if (status === "script_error") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-amber-700 border-amber-300 bg-amber-50"
      >
        <Zap className="h-3 w-3 mr-1" />
        Script error
      </Badge>
    )
  }
  return (
    <Badge
      variant="outline"
      className="text-xs text-red-600 border-red-300 bg-red-50"
    >
      <X className="h-3 w-3 mr-1" />
      Error
    </Badge>
  )
}

function formatDuration(durationMs: number | null): string {
  if (durationMs == null) return "—"
  if (durationMs < 1000) return `${durationMs}ms`
  return `${(durationMs / 1000).toFixed(2)}s`
}

function LogRow({ log }: { log: AgentWebhookLogPublic }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30 gap-2">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <LogStatusBadge status={log.status} />
          <Badge variant="outline" className="text-xs shrink-0">
            {log.webhook_type === "script" ? "Script" : "Session"}
          </Badge>
          <span className="text-xs text-muted-foreground shrink-0">
            {formatExecutedAt(log.executed_at)}
          </span>
          <span className="text-xs text-muted-foreground shrink-0">
            • {formatDuration(log.duration_ms)}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs shrink-0"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? (
            <ChevronUp className="h-3 w-3" />
          ) : (
            <ChevronDown className="h-3 w-3" />
          )}
          {expanded ? "Hide" : "View"}
        </Button>
      </div>

      {expanded && (
        <div className="px-3 py-2 space-y-3 bg-background border-t text-xs">
          {log.remote_ip && (
            <div className="flex items-center gap-2">
              <span className="font-medium text-muted-foreground">From:</span>
              <code className="font-mono">{log.remote_ip}</code>
            </div>
          )}

          {log.payload_received && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Payload received{" "}
                {log.payload_content_type && (
                  <span className="font-normal">
                    ({log.payload_content_type})
                  </span>
                )}
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.payload_received}
              </pre>
            </div>
          )}

          {log.headers_subset && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Forwarded headers
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-32 font-mono text-xs whitespace-pre-wrap break-words">
                {JSON.stringify(log.headers_subset, null, 2)}
              </pre>
            </div>
          )}

          {log.prompt_used && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Prompt used
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.prompt_used}
              </pre>
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
                stdout
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.command_output}
              </pre>
            </div>
          )}

          {log.command_stderr && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                stderr
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.command_stderr}
              </pre>
            </div>
          )}

          {log.error_message && (
            <div>
              <div className="font-medium text-red-600 mb-1 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                Error
              </div>
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

export function WebhookLogsModal({
  agentId,
  webhook,
  open,
  onOpenChange,
}: WebhookLogsModalProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["agent-webhook-logs", webhook?.id],
    queryFn: () =>
      AgentWebhooksService.listWebhookLogs({
        agentId,
        webhookPk: webhook!.id,
        limit: 50,
      }),
    enabled: open && !!webhook,
  })

  const logs = data?.data ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <History className="h-5 w-5" />
            Execution Logs
            {webhook && (
              <span className="font-normal text-muted-foreground">
                — &quot;{webhook.name}&quot;
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            Last 50 webhook invocations.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-y-auto space-y-2 pr-1">
          {isLoading ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              Loading logs...
            </p>
          ) : logs.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No invocations yet. Logs appear after the first webhook call.
            </p>
          ) : (
            logs.map((log) => <LogRow key={log.id} log={log} />)
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
