import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  RefreshCw,
  RotateCcw,
  ExternalLink,
  HeartPulse,
} from "lucide-react"

import type { AgentPublic } from "@/client"
import { AgentsService } from "@/client"
import { cn } from "@/lib/utils"
import useCustomToast from "@/hooks/useCustomToast"
import {
  useAgentStatus,
  severityDotClass,
  severityLabel,
} from "@/hooks/useAgentStatus"
import { RelativeTime } from "@/components/Common/RelativeTime"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { AgentStatusDialog } from "./AgentStatusDialog"

const DEFAULT_STATUS_REFRESH_COMMAND = "/run:status"

interface AgentStatusCardProps {
  agent: AgentPublic
}

/**
 * Configuration-tab card for an agent's self-reported status.
 *
 * - Shows the current cached snapshot (severity dot, summary, timestamps).
 * - Refresh button runs the configured pre-command then re-fetches STATUS.md.
 * - Editable status_refresh_command input persisted via PATCH /agents/{id}.
 * - Surfaces the backend `refresh_command_warning` (source of truth) and an
 *   optional advisory client-side `/run:` not-found hint.
 *
 * Owner-only — mounted in AgentConfigTab behind the `showOperationalSettings`
 * (developer-tier) gate, alongside Schedules and Handovers.
 */
export function AgentStatusCard({ agent }: AgentStatusCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { status, isLoading, forceRefresh, isRefreshing } = useAgentStatus(
    agent.id
  )

  const [command, setCommand] = useState(
    agent.status_refresh_command ?? DEFAULT_STATUS_REFRESH_COMMAND
  )
  const [dialogOpen, setDialogOpen] = useState(false)

  // Re-seed the local input when the persisted value changes (e.g. after the
  // agent query refetches with a freshly-saved command).
  useEffect(() => {
    setCommand(agent.status_refresh_command ?? DEFAULT_STATUS_REFRESH_COMMAND)
  }, [agent.status_refresh_command])

  const saveMutation = useMutation({
    mutationFn: (value: string) =>
      AgentsService.updateAgent({
        id: agent.id,
        requestBody: { status_refresh_command: value },
      }),
    onSuccess: () => {
      showSuccessToast("Status refresh command saved")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
    },
    onError: (error: any) => {
      showErrorToast(error?.message || "Failed to save status refresh command")
    },
  })

  const trimmedCommand = command.trim()
  const persisted = agent.status_refresh_command ?? DEFAULT_STATUS_REFRESH_COMMAND
  const isDirty = trimmedCommand !== persisted.trim()

  const handleSave = () => {
    saveMutation.mutate(trimmedCommand)
  }

  const handleReset = () => {
    setCommand(DEFAULT_STATUS_REFRESH_COMMAND)
  }

  const hasNeverPublished =
    !isLoading && (!status || (status.severity == null && status.raw == null))
  const warning = status?.refresh_command_warning ?? null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HeartPulse className="h-5 w-5" />
          Agent status
        </CardTitle>
        <CardDescription>
          Self-reported health from STATUS.md. Configure a command to refresh it
          on demand.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Status summary row */}
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            {isLoading ? (
              <div className="h-4 w-40 animate-pulse rounded bg-muted" />
            ) : hasNeverPublished ? (
              <p className="text-sm text-muted-foreground">
                No status published yet
              </p>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "inline-block h-2.5 w-2.5 shrink-0 rounded-full",
                      severityDotClass(status?.severity)
                    )}
                  />
                  <span className="text-sm font-medium">
                    {severityLabel(status?.severity)}
                  </span>
                  {status?.summary && (
                    <span className="truncate text-sm text-muted-foreground">
                      — {status.summary}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                  {status?.reported_at && (
                    <span>
                      Reported{" "}
                      <RelativeTime timestamp={status.reported_at} showTooltip />
                    </span>
                  )}
                  {status?.fetched_at && (
                    <span>
                      Fetched{" "}
                      <RelativeTime timestamp={status.fetched_at} showTooltip />
                    </span>
                  )}
                </div>
              </>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {!hasNeverPublished && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setDialogOpen(true)}
                className="gap-1.5"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                View
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={forceRefresh}
              disabled={isRefreshing}
              className="gap-1.5"
            >
              <RefreshCw
                className={cn("h-3.5 w-3.5", isRefreshing && "animate-spin")}
              />
              Refresh
            </Button>
          </div>
        </div>

        {/* Backend warning banner (authoritative) */}
        {warning && (
          <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-700/50 dark:bg-amber-950/40 dark:text-amber-300">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{warning}</span>
          </div>
        )}

        {/* Command input */}
        <div className="space-y-2">
          <p className="text-sm font-medium">Status refresh command</p>
          <div className="flex gap-2">
            <Input
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder={DEFAULT_STATUS_REFRESH_COMMAND}
              maxLength={1024}
              className="font-mono text-sm"
            />
            <Button
              variant="outline"
              size="icon"
              onClick={handleReset}
              disabled={trimmedCommand === DEFAULT_STATUS_REFRESH_COMMAND}
              title="Reset to default"
            >
              <RotateCcw className="h-4 w-4" />
            </Button>
            <Button
              onClick={handleSave}
              disabled={!isDirty || saveMutation.isPending}
            >
              Save
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Runs inside the agent container before each manual or forced status
            refresh. Use a raw shell/python command, or{" "}
            <code className="rounded bg-muted px-1 py-0.5">{"/run:<name>"}</code>{" "}
            to run a command from <code className="rounded bg-muted px-1 py-0.5">CLI_COMMANDS.yaml</code>.
            Failures are non-blocking — the status still refreshes and any
            problem is shown above. Default is{" "}
            <code className="rounded bg-muted px-1 py-0.5">
              {DEFAULT_STATUS_REFRESH_COMMAND}
            </code>
            .
          </p>
        </div>
      </CardContent>

      <AgentStatusDialog
        agentId={agent.id}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
      />
    </Card>
  )
}
