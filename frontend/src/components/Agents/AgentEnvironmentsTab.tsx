import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { AgentsService } from "@/client"
import { EnvironmentCard } from "@/components/Environments/EnvironmentCard"
import { AddEnvironment } from "@/components/Environments/AddEnvironment"
import useCustomToast from "@/hooks/useCustomToast"

interface AgentEnvironmentsTabProps {
  agentId: string
}

export function AgentEnvironmentsTab({ agentId }: AgentEnvironmentsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

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
      showSuccessToast("This environment is now active for new sessions.")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to activate environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
  })

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
                onActivate={() => activateMutation.mutate(activeEnvironment.id)}
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
                    onActivate={() => activateMutation.mutate(env.id)}
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
