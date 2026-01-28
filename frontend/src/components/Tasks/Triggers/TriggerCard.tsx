import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  Clock,
  CalendarClock,
  Webhook,
  Copy,
  Check,
  EllipsisVertical,
  Trash2,
  RefreshCw,
  Loader2,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { TaskTriggersApi } from "./triggerApi"
import type { TaskTriggerPublic, TaskTriggerPublicWithToken } from "./triggerApi"
import { WebhookTokenDisplay } from "./WebhookTokenDisplay"
import useCustomToast from "@/hooks/useCustomToast"

interface TriggerCardProps {
  trigger: TaskTriggerPublic
  taskId: string
}

export function TriggerCard({ trigger, taskId }: TriggerCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [regenerateDialogOpen, setRegenerateDialogOpen] = useState(false)
  const [regeneratedToken, setRegeneratedToken] = useState<TaskTriggerPublicWithToken | null>(null)
  const [copiedUrl, setCopiedUrl] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const toggleMutation = useMutation({
    mutationFn: () =>
      TaskTriggersApi.updateTrigger(taskId, trigger.id, {
        enabled: !trigger.enabled,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to toggle trigger")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => TaskTriggersApi.deleteTrigger(taskId, trigger.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
      showSuccessToast("Trigger deleted")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to delete trigger")
    },
  })

  const regenerateMutation = useMutation({
    mutationFn: () => TaskTriggersApi.regenerateToken(taskId, trigger.id),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
      setRegeneratedToken(result)
      showSuccessToast("Token regenerated")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to regenerate token")
    },
  })

  const copyUrl = async () => {
    if (trigger.webhook_url) {
      await navigator.clipboard.writeText(trigger.webhook_url)
      setCopiedUrl(true)
      setTimeout(() => setCopiedUrl(false), 2000)
    }
  }

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return null
    return new Date(dateStr).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    })
  }

  const formatRelativeTime = (dateStr: string | null) => {
    if (!dateStr) return null
    const date = new Date(dateStr)
    const now = new Date()
    const diff = date.getTime() - now.getTime()
    if (diff < 0) return "overdue"
    const hours = Math.floor(diff / (1000 * 60 * 60))
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60))
    if (hours > 24) {
      const days = Math.floor(hours / 24)
      return `in ${days}d ${hours % 24}h`
    }
    if (hours > 0) return `in ${hours}h ${minutes}m`
    return `in ${minutes}m`
  }

  // Type-specific icon and badge
  const typeConfig = {
    schedule: { icon: Clock, badge: "Schedule", badgeClass: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" },
    exact_date: { icon: CalendarClock, badge: "Exact Date", badgeClass: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300" },
    webhook: { icon: Webhook, badge: "Webhook", badgeClass: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300" },
  }

  const config = typeConfig[trigger.type]
  const Icon = config.icon

  return (
    <>
      <div className="rounded-lg border p-3 space-y-2">
        {/* Header row */}
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="font-medium text-sm truncate flex-1">{trigger.name}</span>
          <Badge variant="outline" className={`text-xs shrink-0 ${config.badgeClass}`}>
            {config.badge}
          </Badge>
          <Switch
            checked={trigger.enabled}
            onCheckedChange={() => toggleMutation.mutate()}
            disabled={toggleMutation.isPending}
          />
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" className="h-7 w-7">
                <EllipsisVertical className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {trigger.type === "webhook" && (
                <DropdownMenuItem
                  onClick={() => {
                    setMenuOpen(false)
                    setRegenerateDialogOpen(true)
                  }}
                >
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Regenerate Token
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onClick={() => {
                  setMenuOpen(false)
                  setDeleteDialogOpen(true)
                }}
                className="text-destructive focus:text-destructive"
              >
                <Trash2 className="h-4 w-4 mr-2" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* Type-specific info */}
        <div className="text-xs text-muted-foreground space-y-0.5 pl-6">
          {trigger.type === "schedule" && (
            <>
              {trigger.schedule_description && <p>{trigger.schedule_description}</p>}
              {trigger.next_execution && (
                <p>
                  Next: {formatDate(trigger.next_execution)}{" "}
                  <span className="text-muted-foreground/60">
                    ({formatRelativeTime(trigger.next_execution)})
                  </span>
                </p>
              )}
              {trigger.last_execution && (
                <p>Last run: {formatDate(trigger.last_execution)}</p>
              )}
            </>
          )}

          {trigger.type === "exact_date" && (
            <>
              <p>
                {formatDate(trigger.execute_at)}
                {trigger.executed ? (
                  <Badge variant="outline" className="ml-2 text-xs bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
                    Executed
                  </Badge>
                ) : (
                  <span className="ml-2 text-muted-foreground/60">
                    ({formatRelativeTime(trigger.execute_at)})
                  </span>
                )}
              </p>
            </>
          )}

          {trigger.type === "webhook" && (
            <>
              <p>Token: {trigger.webhook_token_prefix}...</p>
              {trigger.webhook_url && (
                <div className="flex items-center gap-1">
                  <code className="text-xs truncate max-w-[300px]">{trigger.webhook_url}</code>
                  <button
                    onClick={copyUrl}
                    className="p-0.5 rounded hover:bg-accent transition-colors"
                  >
                    {copiedUrl ? (
                      <Check className="h-3 w-3 text-green-500" />
                    ) : (
                      <Copy className="h-3 w-3" />
                    )}
                  </button>
                </div>
              )}
              {trigger.last_execution && (
                <p>Last invoked: {formatDate(trigger.last_execution)}</p>
              )}
            </>
          )}

          {trigger.payload_template && (
            <p className="text-muted-foreground/60 truncate">
              Payload: {trigger.payload_template}
            </p>
          )}
        </div>
      </div>

      {/* Regenerated token display */}
      {regeneratedToken && regeneratedToken.webhook_token && regeneratedToken.webhook_url && (
        <div className="mt-2">
          <WebhookTokenDisplay
            token={regeneratedToken.webhook_token}
            webhookUrl={regeneratedToken.webhook_url}
          />
          <div className="flex justify-end mt-2">
            <Button size="sm" variant="outline" onClick={() => setRegeneratedToken(null)}>
              Dismiss
            </Button>
          </div>
        </div>
      )}

      {/* Delete confirmation dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Trigger</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{trigger.name}"? This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Delete"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Regenerate token confirmation dialog */}
      <AlertDialog open={regenerateDialogOpen} onOpenChange={setRegenerateDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Regenerate Token</AlertDialogTitle>
            <AlertDialogDescription>
              This will invalidate the current token. Any integrations using the old token will stop working.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => regenerateMutation.mutate()}
            >
              {regenerateMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Regenerate"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
