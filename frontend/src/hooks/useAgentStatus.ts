import { useCallback, useEffect } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { AgentsService } from "@/client"
import type { AgentStatusPublic } from "@/client"
import { eventService } from "@/services/eventService"
import { EventTypes } from "@/services/eventService"

/**
 * React Query hook for fetching and refreshing an agent's self-reported status snapshot.
 *
 * - Fetches from GET /api/v1/agents/{agentId}/status
 * - Query key: ["agentStatus", agentId]
 * - Polls every 60 s while the dialog is open (pass `dialogOpen: true`)
 * - Subscribes to `agent_status_updated` WS events and invalidates on receipt
 * - Force-refresh mutation swallows 429 responses (rate-limited; falls back to cache)
 */
export function useAgentStatus(agentId: string, dialogOpen = false) {
  const queryClient = useQueryClient()

  const queryKey = ["agentStatus", agentId]

  const { data, isLoading, isError } = useQuery<AgentStatusPublic>({
    queryKey,
    queryFn: () => AgentsService.getAgentStatus({ agentId }),
    enabled: !!agentId,
    refetchInterval: dialogOpen ? 60_000 : false,
    staleTime: 30_000,
  })

  // Force-refresh mutation: triggers a live fetch from the container.
  // Silently falls back to cached data on 429 (rate-limited).
  const forceRefreshMutation = useMutation({
    mutationFn: () =>
      AgentsService.getAgentStatus({ agentId, forceRefresh: true }),
    onSuccess: (freshData) => {
      queryClient.setQueryData(queryKey, freshData)
    },
    onError: (error: unknown) => {
      // Swallow 429 rate-limit responses silently; surface other errors.
      const status = (error as { status?: number })?.status
      if (status === 429) {
        return
      }
      console.error("[useAgentStatus] force-refresh failed:", error)
    },
  })

  // Subscribe to WebSocket nudges and invalidate the cache on receipt.
  useEffect(() => {
    if (!agentId) return

    const subId = eventService.subscribe(EventTypes.AGENT_STATUS_UPDATED, (event) => {
      if (!event.model_id || event.model_id === agentId) {
        queryClient.invalidateQueries({ queryKey })
      }
    })

    return () => {
      eventService.unsubscribe(subId)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, queryClient])

  const forceRefresh = useCallback(() => {
    forceRefreshMutation.mutate()
  }, [forceRefreshMutation])

  return {
    status: data ?? null,
    isLoading,
    isError,
    forceRefresh,
    isRefreshing: forceRefreshMutation.isPending,
  }
}

/**
 * Helper: returns true when the status transition happened within the last hour.
 */
export function isRecentTransition(status: AgentStatusPublic): boolean {
  if (!status.severity_changed_at) return false
  try {
    const changedAt = new Date(
      status.severity_changed_at.endsWith("Z")
        ? status.severity_changed_at
        : `${status.severity_changed_at}Z`
    )
    const ageMs = Date.now() - changedAt.getTime()
    return ageMs <= 60 * 60 * 1000 // 1 hour
  } catch {
    return false
  }
}

/**
 * Returns the Tailwind class for the severity dot color.
 */
export function severityDotClass(severity: string | null | undefined): string {
  switch (severity) {
    case "ok":
      return "bg-emerald-500"
    case "info":
      return "bg-sky-500"
    case "warning":
      return "bg-amber-500"
    case "error":
      return "bg-rose-500"
    default:
      return "bg-muted-foreground"
  }
}

/**
 * Returns a human-readable label for the severity.
 */
export function severityLabel(severity: string | null | undefined): string {
  switch (severity) {
    case "ok":
      return "OK"
    case "info":
      return "Info"
    case "warning":
      return "Warning"
    case "error":
      return "Error"
    case "unknown":
      return "Unknown"
    default:
      return "Unknown"
  }
}
