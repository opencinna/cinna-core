import { useEffect } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { AgentApiService } from "@/client"
import { eventService, EventTypes } from "@/services/eventService"

/**
 * Status shape returned by GET /agents/{id}/agent-api/_status.
 * The endpoint is typed `unknown` in the generated client, so we narrow here.
 */
export interface AgentApiStatus {
  state: "disabled" | "not_running" | "running" | "error" | "stopped" | "empty"
  agent_api_enabled: boolean
  spec_available: boolean
  last_error: string | null
  policy?: Record<string, unknown> | null
  child_running?: boolean
  has_app?: boolean
  env_status?: string
}

/**
 * Fetches the agent REST API build/run status and live-refreshes it via the
 * AGENT_API_STATUS_CHANGED WebSocket event (emitted on build success/failure
 * /reload), mirroring useAgentStatus.
 *
 * Query key: ["agentApiStatus", agentId]
 */
export function useAgentApiStatus(agentId: string, enabled = true) {
  const queryClient = useQueryClient()
  const queryKey = ["agentApiStatus", agentId]

  const query = useQuery<AgentApiStatus>({
    queryKey,
    queryFn: () =>
      AgentApiService.getAgentApiStatus({ agentId }) as Promise<AgentApiStatus>,
    enabled: !!agentId && enabled,
    staleTime: 15_000,
  })

  useEffect(() => {
    if (!agentId) return
    const subId = eventService.subscribe(
      EventTypes.AGENT_API_STATUS_CHANGED,
      (event) => {
        if (!event.model_id || event.model_id === agentId) {
          queryClient.invalidateQueries({ queryKey })
        }
      },
    )
    return () => eventService.unsubscribe(subId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, queryClient])

  return query
}
