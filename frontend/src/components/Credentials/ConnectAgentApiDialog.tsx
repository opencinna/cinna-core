import { useMemo } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { AgentApiService, AgentsService } from "@/client"
import {
  AgentSelectorDialog,
  type AgentOption,
} from "@/components/Common/AgentSelectorDialog"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface ConnectAgentApiDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Optional consumer agent to link the new credential to immediately. */
  defaultConsumerAgentId?: string
  /** Called after a successful connect (e.g. to refresh the parent list). */
  onConnected?: () => void
}

/**
 * "Connect Agent API" — pick a producer agent that exposes a REST API; we mint
 * the proxy token and create the ``agent_api`` connection credential (linking it
 * to the consumer agent when one is provided). Uses the shared agent selector so
 * producers show with their normal Bot badge + colour.
 */
export function ConnectAgentApiDialog({
  open,
  onOpenChange,
  defaultConsumerAgentId,
  onConnected,
}: ConnectAgentApiDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: agentsData } = useQuery({
    queryKey: ["agents", "connect-agent-api"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: open,
  })

  // Only agents that expose a REST API can be producers — and an agent cannot
  // connect to its own API (exclude the current consumer agent).
  const producers = useMemo<AgentOption[]>(
    () =>
      (agentsData?.data ?? [])
        .filter((a) => a.agent_api_enabled && a.id !== defaultConsumerAgentId)
        .map((a) => ({
          id: a.id,
          name: a.name,
          colorPreset: a.ui_color_preset,
        })),
    [agentsData, defaultConsumerAgentId],
  )

  const connectMutation = useMutation({
    mutationFn: (producerId: string) =>
      AgentApiService.connectAgentApi({
        agentId: producerId,
        requestBody: { consumer_agent_id: defaultConsumerAgentId ?? null },
      }),
    onSuccess: (_data, producerId) => {
      showSuccessToast("Connected — credential created")
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({
        queryKey: ["agentApiConnections", producerId],
      })
      onOpenChange(false)
      onConnected?.()
    },
    onError: (err) => handleError.bind(showErrorToast)(err as any),
  })

  return (
    <AgentSelectorDialog
      open={open}
      onOpenChange={onOpenChange}
      onSelect={(agentId) => {
        if (agentId) connectMutation.mutate(agentId)
      }}
      agents={producers}
      title="Connect Agent API"
      closeOnSelect={false}
    />
  )
}
