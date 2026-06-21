import { useState, useEffect } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { AgentsService } from "@/client"
import { eventService, EventTypes, type EventData } from "@/services/eventService"
import type { AgentEnvironmentPublic } from "@/client"
import { EnvironmentCard } from "@/components/Environments/EnvironmentCard"
import { EnvironmentConsoleDrawer } from "@/components/Environments/EnvironmentConsoleDrawer"
import { AddEnvironment } from "@/components/Environments/AddEnvironment"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import useRole from "@/hooks/useRole"
import type { EnvConsoleKind } from "@/hooks/useEnvConsoleSocket"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const INACTIVITY_OPTIONS = [
  { value: "default", label: "10 minutes (default)" },
  { value: "2_days", label: "2 days" },
  { value: "1_week", label: "1 week" },
  { value: "1_month", label: "1 month" },
  { value: "always_on", label: "Always On" },
] as const

interface AgentEnvironmentsTabProps {
  agentId: string
}

export function AgentEnvironmentsTab({ agentId }: AgentEnvironmentsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { user } = useAuth()
  // Phase 3 — agent-user view of this tab is read-only.  Activate /
  // add / inactivity-edit are developer-only controls; the EnvironmentCard
  // still surfaces sessions and start-conversation links so the install→
  // chat flow works.
  const { isDeveloper } = useRole()

  // Shared console drawer state — a single drawer instance is hosted here and
  // driven by the per-card Logs/Terminal actions.
  const [consoleTarget, setConsoleTarget] = useState<{
    environment: AgentEnvironmentPublic
    kind: EnvConsoleKind
  } | null>(null)

  const { data: agentData } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.readAgent({ id: agentId }),
    enabled: !!agentId,
  })

  const updateInactivityMutation = useMutation({
    mutationFn: (value: string) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: {
          inactivity_period_limit: value === "default" ? null : value,
        },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      showSuccessToast("Inactivity period updated.")
    },
    onError: () => {
      showErrorToast("Failed to update inactivity period")
    },
  })

  const { data: environmentsData, isLoading } = useQuery({
    queryKey: ["environments", agentId],
    queryFn: () => AgentsService.listAgentEnvironments({ id: agentId }),
    enabled: !!agentId,
    refetchInterval: 10000, // Poll every 10s as a fallback for status updates
  })

  // Real-time status updates: the backend emits environment lifecycle events
  // (e.g. ENVIRONMENT_STATUS_CHANGED at the very start of a rebuild) so the
  // cards reflect rebuild/suspend/activate transitions immediately instead of
  // waiting for the 10s poll or a blocking mutation to settle.
  useEffect(() => {
    if (!agentId) return
    const handler = (event: EventData) => {
      // Each env event carries the owning agent id in its meta — ignore events
      // for other agents to avoid needless refetches.
      const eventAgentId = event.meta?.agent_id as string | undefined
      if (eventAgentId && eventAgentId !== agentId) return
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      // A critical-state transition also changes the env's action log, which is
      // surfaced by the per-card "Show details" modal — refresh it so an open
      // modal reflects the new entry immediately.
      if (event.type === EventTypes.ENVIRONMENT_CRITICAL_STATE_CHANGED) {
        const environmentId = event.meta?.environment_id as string | undefined
        if (environmentId) {
          queryClient.invalidateQueries({
            queryKey: ["env-action-logs", environmentId],
          })
        }
      }
    }
    const subIds = [
      EventTypes.ENVIRONMENT_STATUS_CHANGED,
      EventTypes.ENVIRONMENT_CRITICAL_STATE_CHANGED,
      EventTypes.ENVIRONMENT_ACTIVATING,
      EventTypes.ENVIRONMENT_ACTIVATED,
      EventTypes.ENVIRONMENT_ACTIVATION_FAILED,
      EventTypes.ENVIRONMENT_SUSPENDED,
    ].map((type) => eventService.subscribe(type, handler))
    return () => subIds.forEach((id) => eventService.unsubscribe(id))
  }, [agentId, queryClient])

  const activateMutation = useMutation({
    mutationFn: (envId: string) =>
      AgentsService.activateEnvironment({ id: agentId, envId }),
    onSuccess: () => {
      showSuccessToast("Environment activated successfully.")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to activate environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
  })

  const handleActivate = (envId: string) => {
    const environments = environmentsData?.data || []

    // If only one environment, don't ask for confirmation
    if (environments.length === 1) {
      activateMutation.mutate(envId)
      return
    }

    // If multiple environments, ask for confirmation
    if (
      confirm(
        "Activating this environment will start it and stop all other environments. Continue?"
      )
    ) {
      activateMutation.mutate(envId)
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="flex justify-between items-center">
          <div>
            <h2 className="text-2xl font-bold">Environments</h2>
            <p className="text-muted-foreground">
              Manage runtime environments for your agent
            </p>
          </div>
        </div>
        <div className="text-center py-8 text-muted-foreground">Loading environments...</div>
      </div>
    )
  }

  const environments = environmentsData?.data || []
  // Use ``agent.active_environment_id`` as the source of truth for which
  // env is "the primary one". During install/auto-start the env's own
  // ``is_active`` flag stays ``false`` until the background Docker build
  // completes — but the agent already points at the new env from the
  // moment ``InstallService`` creates it, so this surface the still-
  // building env immediately. ``is_active`` is a fallback for any older
  // row whose pointer was lost.
  const activeEnvironment =
    environments.find((env) => env.id === agentData?.active_environment_id) ??
    environments.find((env) => env.is_active)
  const inactiveEnvironments = environments
    .filter((env) => env.id !== activeEnvironment?.id)
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())

  // Single ordered list rendered as a tiled grid: the primary (active)
  // environment is always first, followed by the rest (most-recently-updated
  // first). Agent-users (read-only) only ever see the active environment.
  const orderedEnvironments = isDeveloper
    ? [...(activeEnvironment ? [activeEnvironment] : []), ...inactiveEnvironments]
    : activeEnvironment
      ? [activeEnvironment]
      : []

  // Console gating (UX only — the backend WS dep is the real boundary):
  //   * Logs     → owner (or superuser, who already reads as developer here).
  //   * Terminal → owner AND agent-developer/superuser.
  // ``isDeveloper`` already degrades to false for foreign installs via the
  // RoleOverrideContext, so an installed (non-owned) agent never shows the
  // terminal. We additionally require ownership of the agent itself.
  const isOwner = !!user && agentData?.owner_id === user.id
  const canFollowLogs = isDeveloper && isOwner
  const canOpenTerminal = isDeveloper && isOwner

  const openConsole = (environmentId: string, kind: EnvConsoleKind) => {
    const env = environments.find((e) => e.id === environmentId)
    if (env) setConsoleTarget({ environment: env, kind })
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold">Environments</h2>
          <p className="text-muted-foreground">
            {isDeveloper
              ? "Manage runtime environments for your agent. One environment must be active and running to create new sessions."
              : "Active environment for this agent. Use it to start conversations."}
          </p>
        </div>
        {isDeveloper && <AddEnvironment agentId={agentId} />}
      </div>

      {isDeveloper && (
        <div className="flex items-center gap-6 flex-wrap">
          <div className="flex items-center gap-3">
            <label
              htmlFor="inactivity-period"
              className="text-sm font-medium text-muted-foreground whitespace-nowrap"
            >
              Auto-suspend after inactivity
            </label>
            <Select
              value={agentData?.inactivity_period_limit ?? "default"}
              onValueChange={(value) => updateInactivityMutation.mutate(value)}
              disabled={updateInactivityMutation.isPending}
            >
              <SelectTrigger id="inactivity-period" className="w-[200px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {INACTIVITY_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      )}

      {environments.length === 0 ? (
        <div className="text-center py-12 border-2 border-dashed rounded-lg">
          <p className="text-muted-foreground mb-4">No environments yet</p>
          {isDeveloper && <AddEnvironment agentId={agentId} />}
        </div>
      ) : (
        // Tiled list — each card is half the page width on wider screens; the
        // active environment is always rendered first with a highlighted border.
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {orderedEnvironments.map((env) => (
            <EnvironmentCard
              key={env.id}
              environment={env}
              agentId={agentId}
              isPrimary={env.id === activeEnvironment?.id}
              onActivate={isDeveloper ? () => handleActivate(env.id) : undefined}
              readOnly={!isDeveloper}
              canFollowLogs={canFollowLogs}
              canOpenTerminal={canOpenTerminal}
              onOpenConsole={openConsole}
            />
          ))}
        </div>
      )}

      {consoleTarget && (
        <EnvironmentConsoleDrawer
          open
          onOpenChange={(next) => {
            if (!next) setConsoleTarget(null)
          }}
          environment={consoleTarget.environment}
          kind={consoleTarget.kind}
        />
      )}
    </div>
  )
}
