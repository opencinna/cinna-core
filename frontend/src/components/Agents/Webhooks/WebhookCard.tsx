import { useState } from "react"
import {
  Check,
  Clock,
  Copy,
  History,
  MessageSquare,
  Pencil,
  Power,
  PowerOff,
  RefreshCw,
  Terminal,
  Trash2,
} from "lucide-react"

import type { AgentWebhookPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"

interface WebhookCardProps {
  webhook: AgentWebhookPublic
  onEdit: (webhook: AgentWebhookPublic) => void
  onShowLogs: (webhook: AgentWebhookPublic) => void
  onToggleEnabled: (webhook: AgentWebhookPublic) => void
  onRegenerateToken: (webhook: AgentWebhookPublic) => void
  onDelete: (webhook: AgentWebhookPublic) => void
  pendingAction?: boolean
}

function formatRelative(isoDate: string | null | undefined): string | null {
  if (!isoDate) return null
  try {
    let utc = isoDate
    if (!utc.endsWith("Z") && !utc.includes("+") && utc.includes("T")) {
      utc = utc + "Z"
    }
    const date = new Date(utc)
    const diffMs = Date.now() - date.getTime()
    const diffSec = Math.floor(diffMs / 1000)
    if (diffSec < 60) return `${diffSec}s ago`
    const diffMin = Math.floor(diffSec / 60)
    if (diffMin < 60) return `${diffMin}m ago`
    const diffHr = Math.floor(diffMin / 60)
    if (diffHr < 24) return `${diffHr}h ago`
    const diffD = Math.floor(diffHr / 24)
    if (diffD < 7) return `${diffD}d ago`
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return null
  }
}

function maskUrl(url: string | null | undefined): string {
  if (!url) return ""
  try {
    const parsed = new URL(url)
    const last = parsed.pathname.split("/").pop() ?? ""
    return `${parsed.origin}/…/${last}`
  } catch {
    // Best-effort: keep last path segment.
    const idx = url.lastIndexOf("/")
    if (idx === -1) return url
    return `…${url.slice(idx)}`
  }
}

export function WebhookCard({
  webhook,
  onEdit,
  onShowLogs,
  onToggleEnabled,
  onRegenerateToken,
  onDelete,
  pendingAction,
}: WebhookCardProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copiedUrl, setCopiedUrl] = useState(false)

  const isScript = webhook.type === "script"
  const url = webhook.webhook_url ?? ""

  const handleCopyUrl = async () => {
    if (!url) return
    try {
      await navigator.clipboard.writeText(url)
      setCopiedUrl(true)
      showSuccessToast("Webhook URL copied")
      setTimeout(() => setCopiedUrl(false), 2000)
    } catch {
      showErrorToast("Failed to copy URL")
    }
  }

  const lastFired = formatRelative(webhook.last_execution)

  return (
    <div
      className={`flex items-start justify-between gap-2 px-3 py-2 border rounded-lg ${
        !webhook.enabled ? "bg-muted" : ""
      }`}
    >
      {/* Left side dims when disabled; the action cluster on the right
          stays full-contrast so the toggle remains visually live. */}
      <div
        className={`min-w-0 flex-1 space-y-1 ${
          !webhook.enabled ? "opacity-60" : ""
        }`}
      >
        <div className="flex items-center gap-2 flex-wrap">
          {isScript ? (
            <Terminal className="h-4 w-4 text-amber-600 shrink-0" />
          ) : (
            <MessageSquare className="h-4 w-4 text-primary shrink-0" />
          )}
          <span className="font-medium text-sm truncate">{webhook.name}</span>
          {webhook.enabled ? (
            <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
              Enabled
            </Badge>
          ) : (
            <Badge variant="secondary" className="text-xs shrink-0">
              Disabled
            </Badge>
          )}
          {isScript ? (
            <Badge
              variant="outline"
              className="text-xs shrink-0 text-amber-700 border-amber-300 bg-amber-50"
            >
              Script
            </Badge>
          ) : (
            <Badge variant="outline" className="text-xs shrink-0">
              Session
            </Badge>
          )}
        </div>

        <div className="flex items-center gap-1.5 text-xs">
          <code
            className={`font-mono truncate ${
              webhook.enabled ? "text-muted-foreground" : "text-muted-foreground/60"
            }`}
            title={url}
          >
            {maskUrl(url)}
          </code>
          {url && (
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5 shrink-0"
              onClick={handleCopyUrl}
              title="Copy webhook URL"
            >
              {copiedUrl ? (
                <Check className="h-3 w-3 text-green-500" />
              ) : (
                <Copy className="h-3 w-3" />
              )}
            </Button>
          )}
        </div>

        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>
            Token:{" "}
            <code className="font-mono">{webhook.webhook_token_prefix}…</code>
          </span>
          {lastFired && (
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              Fired {lastFired}
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-0.5 shrink-0">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => onShowLogs(webhook)}
              >
                <History className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Execution logs
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => onEdit(webhook)}
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Edit webhook
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <AlertDialog>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <AlertDialogTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-6 w-6">
                    <RefreshCw className="h-3.5 w-3.5" />
                  </Button>
                </AlertDialogTrigger>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Regenerate token
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Regenerate webhook token</AlertDialogTitle>
              <AlertDialogDescription>
                The current token will stop working immediately. Any external
                system using it will need to be updated with the new token.
                Continue?
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={() => onRegenerateToken(webhook)}>
                Regenerate
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={() => onToggleEnabled(webhook)}
                disabled={pendingAction}
              >
                {webhook.enabled ? (
                  <Power className="h-3.5 w-3.5 text-emerald-500" />
                ) : (
                  <PowerOff className="h-3.5 w-3.5 text-muted-foreground" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              {webhook.enabled ? "Disable" : "Enable"}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-destructive hover:text-destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete webhook</AlertDialogTitle>
              <AlertDialogDescription>
                Are you sure you want to delete &quot;{webhook.name}&quot;?
                This will also remove all of its execution logs. This action
                cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => onDelete(webhook)}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  )
}
