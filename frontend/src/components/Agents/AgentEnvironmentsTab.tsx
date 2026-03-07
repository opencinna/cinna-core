import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { AgentsService } from "@/client"
import { EnvironmentCard } from "@/components/Environments/EnvironmentCard"
import { AddEnvironment } from "@/components/Environments/AddEnvironment"
import useCustomToast from "@/hooks/useCustomToast"
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
    refetchInterval: 10000, // Poll every 10s for status updates
  })

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
  const activeEnvironment = environments.find((env) => env.is_active)
  const inactiveEnvironments = environments
    .filter((env) => !env.is_active)
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold">Environments</h2>
          <p className="text-muted-foreground">
            Manage runtime environments for your agent. One environment must be active and running
            to create new sessions.
          </p>
        </div>
        <AddEnvironment agentId={agentId} />
      </div>

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

      {environments.length === 0 ? (
        <div className="text-center py-12 border-2 border-dashed rounded-lg">
          <p className="text-muted-foreground mb-4">No environments yet</p>
          <AddEnvironment agentId={agentId} />
        </div>
      ) : (
        <div className="space-y-4">
          {activeEnvironment && (
            <div>
              <h3 className="text-sm font-semibold text-muted-foreground mb-2">
                ACTIVE ENVIRONMENT
              </h3>
              <EnvironmentCard
                environment={activeEnvironment}
                agentId={agentId}
                onActivate={() => handleActivate(activeEnvironment.id)}
              />
            </div>
          )}

          {inactiveEnvironments.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-muted-foreground mb-2">
                OTHER ENVIRONMENTS
              </h3>
              <div className="space-y-3">
                {inactiveEnvironments.map((env) => (
                  <EnvironmentCard
                    key={env.id}
                    environment={env}
                    agentId={agentId}
                    onActivate={() => handleActivate(env.id)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
