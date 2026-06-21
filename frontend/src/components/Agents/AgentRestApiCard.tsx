import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileJson, Lock, Network, RefreshCw, Trash2 } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
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
import { AgentApiAccessScopesCard } from "./AgentApiAccessScopesCard"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { useAgentApiStatus } from "@/hooks/useAgentApi"
import useCustomToast from "@/hooks/useCustomToast"
import { openAgentApiSpec } from "@/utils/agentApiSpec"

interface AgentRestApiCardProps {
  agentId: string
  agentApiEnabled: boolean
  /** Producer opt-in for per-user identity + scope grants (L2). */
  agentApiIdentityEnabled: boolean
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
  agentApiIdentityEnabled,
}: AgentRestApiCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [errorDetailsOpen, setErrorDetailsOpen] = useState(false)
  // Refresh is more than a single call: when the producer env is suspended,
  // _refresh wakes it (blocking briefly server-side) and may still report it as
  // booting, so we poll until it is running before reporting success. This local
  // state drives the "Waking up..." progress label and disables the button.
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [refreshLabel, setRefreshLabel] = useState("Refresh")
  const refreshActiveRef = useRef(false)

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

  // Number of _refresh polls before we give up waking a suspended env. Each
  // _refresh blocks up to ~10s server-side while activating, so this is a
  // generous ceiling for a producer that never boots.
  const MAX_REFRESH_POLLS = 12

  useEffect(() => {
    // Abort any in-flight refresh loop on unmount.
    return () => {
      refreshActiveRef.current = false
    }
  }, [])

  const handleRefresh = useCallback(async () => {
    if (refreshActiveRef.current) return
    refreshActiveRef.current = true
    setIsRefreshing(true)
    setRefreshLabel("Refresh")

    const sleep = (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms))

    try {
      let attempt = 0
      while (refreshActiveRef.current && attempt < MAX_REFRESH_POLLS) {
        attempt += 1
        // _refresh wakes a suspended env (blocking briefly), re-harvests when
        // running, and returns the live status.
        const data = (await AgentApiService.refreshAgentApiStatus({
          agentId,
        })) as AgentApiStatus

        if (!refreshActiveRef.current) return

        // Seed the cache with the freshly re-harvested status so a stale boot
        // error / badge clears immediately.
        queryClient.setQueryData(["agentApiStatus", agentId], data)

        // The serving child app being idle/stopped is irrelevant to whether the
        // refresh succeeded — the spec is harvested import-only without it. So
        // the decision keys off the env-lifecycle + spec availability, not the
        // raw child-app `state`. The ONLY "still waking" case is `not_running`
        // (env genuinely not up yet); once the env is running every outcome is
        // terminal:
        //   success → spec harvested + usable
        //   failure → boot/harvest error (`state === "error"` or `last_error`)
        //   empty   → env up, nothing to harvest (no endpoints exposed)
        const envRunning = data.state !== "not_running"
        const harvestFailed = data.state === "error" || !!data.last_error
        const specReady = !!data.spec_available && !data.last_error

        if (envRunning) {
          queryClient.invalidateQueries({
            queryKey: ["agentApiStatus", agentId],
          })
          queryClient.invalidateQueries({
            queryKey: ["agentApiSpec", agentId],
          })
          if (refreshActiveRef.current) {
            if (harvestFailed) {
              showErrorToast(
                data.last_error
                  ? `API failed to build: ${data.last_error}`
                  : "The spec could not be harvested",
              )
            } else if (specReady) {
              showSuccessToast("Agent REST API refreshed")
            } else {
              // Running but nothing to harvest — no endpoints exposed yet.
              showSuccessToast("Refreshed — no endpoints are exposed yet")
            }
          }
          return
        }

        // not_running (env still coming up) — keep waking. Show progress and
        // poll again (the server already blocked while activating).
        setRefreshLabel("Waking up agent...")
        await sleep(1500)
      }

      if (refreshActiveRef.current) {
        // Exhausted the budget without the env coming up.
        queryClient.invalidateQueries({ queryKey: ["agentApiStatus", agentId] })
        showErrorToast(
          "The agent's environment is still starting. Please try again shortly.",
        )
      }
    } catch (e: any) {
      showErrorToast(e?.message || "Failed to refresh")
    } finally {
      refreshActiveRef.current = false
      setIsRefreshing(false)
      setRefreshLabel("Refresh")
    }
  }, [agentId, queryClient, showSuccessToast, showErrorToast])

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
                    onClick={handleRefresh}
                    disabled={isRefreshing}
                    title="Re-check the API (wakes a suspended env, re-harvests the spec, and clears a stale error)"
                  >
                    <RefreshCw
                      className={`h-3 w-3 mr-1 ${isRefreshing ? "animate-spin" : ""}`}
                    />
                    {isRefreshing ? refreshLabel : "Retry"}
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
              onClick={handleRefresh}
              disabled={isRefreshing}
              title="Re-parse the API (wakes a suspended env, re-harvests the spec, and re-reads policy.yaml)"
            >
              <RefreshCw
                className={`h-4 w-4 mr-1 ${isRefreshing ? "animate-spin" : ""}`}
              />
              {isRefreshing ? refreshLabel : "Refresh"}
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
                          // Show the owner's email next to the badge so
                          // identical agent names (e.g. several bundle installs
                          // of the same agent owned by different users) stay
                          // distinguishable.
                          <span
                            key={a.id}
                            className="inline-flex items-center gap-1.5"
                          >
                            <AgentBadge agent={a} />
                            {a.owner_email && (
                              <span className="text-xs text-muted-foreground truncate">
                                {a.owner_email}
                              </span>
                            )}
                          </span>
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

          {/* Access & Scopes — per-user identity + scope grants (L2). */}
          <AgentApiAccessScopesCard
            agentId={agentId}
            identityEnabled={agentApiIdentityEnabled}
          />
        </CardContent>
      )}
    </Card>
  )
}
