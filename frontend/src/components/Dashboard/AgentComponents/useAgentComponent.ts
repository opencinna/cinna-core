import { useQuery } from "@tanstack/react-query"

import { AgentDashboardsService } from "@/client"
import { readComponentError } from "./tileConfig"

const ENV_RETRY_LIMIT = 20
const ENV_RETRY_MS = 3000

export function agentComponentQueryKey(
  agentId: string,
  name: string,
  skill: string | null,
) {
  return ["agentDashboardComponent", agentId, name, skill] as const
}

/**
 * One component of an agent (`GET /agents/{id}/dashboards/{name}`): its view,
 * params, refresh defaults (UQ1), size hint (UQ2) and prompts.
 *
 * The listing needs a running environment and wakes a suspended one, answering
 * `env_unavailable` while it starts; that one error is retried on a slow
 * cadence so a tile opened on a sleeping agent fills in once it is up. The
 * tile, its header and the Edit dialog share this query key.
 */
export function useAgentComponent(
  agentId: string | undefined,
  name: string,
  skill: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: agentComponentQueryKey(agentId ?? "", name, skill),
    queryFn: () =>
      AgentDashboardsService.getAgentDashboardComponent({
        agentId: agentId ?? "",
        name,
        skill,
      }),
    enabled: enabled && !!agentId && !!name,
    staleTime: 60_000,
    retry: (count, error) =>
      readComponentError(error)?.code === "env_unavailable" &&
      count < ENV_RETRY_LIMIT,
    retryDelay: ENV_RETRY_MS,
  })
}
