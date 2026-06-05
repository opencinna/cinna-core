import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileJson, Lock, Network, RefreshCw, Trash2 } from "lucide-react"
import { useState } from "react"
import type { AgentApiProducerConnection } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import type { AgentApiStatus } from "@/hooks/useAgentApi"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { useAgentApiStatus } from "@/hooks/useAgentApi"
import useCustomToast from "@/hooks/useCustomToast"
import { openAgentApiSpec } from "@/utils/agentApiSpec"

interface AgentRestApiCardProps {
  agentId: string
  agentApiEnabled: boolean
}

/** Extracts an HTTP status code (100–599) from a boot/status error string, if present. */
function parseHttpStatus(message: string): number | null {
  const match = message.match(/\b([1-5]\d{2})\b/)
  return match ? Number(match[1]) : null
}

/** Builds a compact, human-friendly summary of a boot/status error. */
function summarizeBootError(message: string): string {
  const code = parseHttpStatus(message)
  if (code === 404)
    return "API failed to start. Error 404. Probably, not implemented yet."
  if (code) return `API failed to start. Error ${code}.`
  return "API failed to start."
}

const STATE_BADGE: Record<string, { label: string; className: string }> = {
  running: { label: "Running", className: "bg-emerald-500" },
  stopped: { label: "Built", className: "bg-sky-500" },
  not_running: { label: "Env stopped", className: "bg-amber-500" },
  empty: { label: "No endpoints", className: "bg-gray-400" },
  error: { label: "Error", className: "bg-rose-500" },
  disabled: { label: "Disabled", className: "bg-gray-400" },
}

export function AgentRestApiCard({
  agentId,
  agentApiEnabled,
}: AgentRestApiCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [errorDetailsOpen, setErrorDetailsOpen] = useState(false)

  const { data: status } = useAgentApiStatus(agentId, agentApiEnabled)

  const { data: connectionsData, isLoading: connectionsLoading } = useQuery({
    queryKey: ["agentApiConnections", agentId],
    queryFn: () => AgentApiService.listAgentApiConnections({ agentId }),
    enabled: agentApiEnabled,
  })
  const connections: AgentApiProducerConnection[] = connectionsData?.data ?? []

  const disconnectMutation = useMutation({
    mutationFn: (tokenId: string) =>
      AgentApiService.deleteAgentApiConnection({ agentId, tokenId }),
    onSuccess: () => {
      showSuccessToast("Disconnected")
      queryClient.invalidateQueries({
        queryKey: ["agentApiConnections", agentId],
      })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to disconnect"),
  })

  const refreshMutation = useMutation({
    mutationFn: () => AgentApiService.refreshAgentApiStatus({ agentId }),
    onSuccess: (data) => {
      // The endpoint returns the freshly re-harvested status; seed the cache
      // with it so a stale boot error clears immediately.
      queryClient.setQueryData(
        ["agentApiStatus", agentId],
        data as AgentApiStatus,
      )
      queryClient.invalidateQueries({ queryKey: ["agentApiStatus", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agentApiSpec", agentId] })
      showSuccessToast("Agent REST API refreshed")
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to refresh"),
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { agent_api_enabled: enabled },
      }),
    onSuccess: () => {
      showSuccessToast("Agent REST API updated")
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agentApiStatus", agentId] })
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to update"),
  })

  const badge = STATE_BADGE[status?.state ?? "disabled"] ?? STATE_BADGE.disabled

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Network className="h-5 w-5" />
              Agent REST API
              {agentApiEnabled && status && (
                <Badge className={`${badge.className} text-white text-xs`}>
                  {badge.label}
                </Badge>
              )}
            </CardTitle>
            <CardDescription>
              {agentApiEnabled
                ? "Expose a capability-narrowed REST API other agents can call as code. Other agents connect to it from their own Credentials tab."
                : "Enable to expose a validated REST API other agents can call as code — for example, a narrow API in front of credentials with excessive permissions."}
            </CardDescription>
          </div>
          <Switch
            checked={agentApiEnabled}
            onCheckedChange={(v) => toggleMutation.mutate(v)}
            disabled={toggleMutation.isPending}
            className="ml-4 mt-1"
          />
        </div>
      </CardHeader>
      {agentApiEnabled && (
        <CardContent className="space-y-4">
          {/* Boot error surfacing */}
          {status?.state === "error" && status.last_error && (
            <div className="rounded-md border border-rose-300 bg-rose-50 dark:bg-rose-950/30 p-3 text-xs">
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium text-rose-700 dark:text-rose-300">
                  {summarizeBootError(status.last_error)}
                </p>
                <div className="flex shrink-0 items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-rose-700 hover:text-rose-800 hover:bg-rose-100 dark:text-rose-300 dark:hover:bg-rose-900/40"
                    onClick={() => refreshMutation.mutate()}
                    disabled={refreshMutation.isPending}
                    title="Re-check the API (re-harvests the spec and clears a stale error)"
                  >
                    <RefreshCw
                      className={`h-3 w-3 mr-1 ${refreshMutation.isPending ? "animate-spin" : ""}`}
                    />
                    Retry
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-rose-700 hover:text-rose-800 hover:bg-rose-100 dark:text-rose-300 dark:hover:bg-rose-900/40"
                    onClick={() => setErrorDetailsOpen((v) => !v)}
                  >
                    {errorDetailsOpen ? "Hide" : "Details"}
                  </Button>
                </div>
              </div>
              {errorDetailsOpen && (
                <pre className="mt-2 whitespace-pre-wrap text-rose-600 dark:text-rose-400 max-h-40 overflow-auto">
                  {status.last_error}
                </pre>
              )}
            </div>
          )}

          {/* View spec + Refresh */}
          <div className="flex items-center gap-2">
            {/* View spec — opens the harvested OpenAPI spec as rendered docs in
                a new tab (authenticated via the same app shell). */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => openAgentApiSpec(agentId)}
              title="Open the OpenAPI spec (rendered docs) in a new tab"
            >
              <FileJson className="h-4 w-4 mr-1" />
              View Spec
            </Button>
            {/* Refresh — force a re-harvest of the spec and re-parse of
                policy.yaml so on-demand edits are picked up immediately. */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => refreshMutation.mutate()}
              disabled={refreshMutation.isPending}
              title="Re-parse the API (re-harvests the spec and re-reads policy.yaml)"
            >
              <RefreshCw
                className={`h-4 w-4 mr-1 ${refreshMutation.isPending ? "animate-spin" : ""}`}
              />
              Refresh
            </Button>
          </div>

          {/* Connections — agents consuming this API */}
          <div>
            <div className="flex items-center gap-2 mb-2">
              <Network className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-medium">Connections</span>
              {connections.length > 0 && (
                <Badge variant="secondary" className="text-xs">
                  {connections.length}
                </Badge>
              )}
            </div>

            {connectionsLoading ? (
              <p className="text-sm text-muted-foreground">
                Loading connections…
              </p>
            ) : connections.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No agents are connected yet. Another agent connects to this API
                from its own Credentials tab ("Connect Agent API").
              </p>
            ) : (
              <div className="space-y-1.5">
                {connections.map((conn) => (
                  <div
                    key={conn.token_id}
                    className="flex items-center justify-between gap-2 px-3 py-2 border rounded-lg"
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-wrap">
                      {conn.consumer_agents.length === 0 ? (
                        <span className="text-sm text-muted-foreground italic">
                          Not linked to an agent
                        </span>
                      ) : (
                        conn.consumer_agents.map((a) => (
                          <AgentBadge key={a.id} agent={a} />
                        ))
                      )}
                      {conn.read_only && (
                        <Badge variant="outline" className="gap-1 text-xs">
                          <Lock className="h-3 w-3" />
                          read-only
                        </Badge>
                      )}
                    </div>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-destructive hover:text-destructive shrink-0"
                          disabled={disconnectMutation.isPending}
                          title="Disconnect (deletes the connection and its token)"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          <span className="sr-only">Disconnect</span>
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>Disconnect?</AlertDialogTitle>
                          <AlertDialogDescription>
                            This deletes the connection and its access token.
                            Any agent using it will immediately lose access to
                            this API. This cannot be undone.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction
                            onClick={() =>
                              disconnectMutation.mutate(conn.token_id)
                            }
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          >
                            Disconnect
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  )
}
