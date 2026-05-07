import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Bot } from "lucide-react"
import { useEffect } from "react"

import { AgentsService } from "@/client"
import AddAgent from "@/components/Agents/AddAgent"
import { AgentCard } from "@/components/Agents/AgentCard"
import PendingItems from "@/components/Pending/PendingItems"
import useRole from "@/hooks/useRole"
import useWorkspace from "@/hooks/useWorkspace"
import { usePageHeader } from "@/routes/_layout"
import { eventService, EventTypes } from "@/services/eventService"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({
    meta: [
      {
        title: `Agents - ${APP_NAME}`,
      },
    ],
  }),
})

function AgentsGrid() {
  const { workspaceFilter } = useWorkspace()
  const queryClient = useQueryClient()

  // Fetch owned agents
  const {
    data,
    isLoading: agentsLoading,
    error: agentsError,
  } = useQuery({
    queryKey: ["agents", workspaceFilter],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey
      const response = await AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
      return response
    },
  })

  // Fetch status snapshots for all agents in one batch (cache-only, cheap).
  const { data: statusesData } = useQuery({
    queryKey: ["agentStatuses", workspaceFilter],
    queryFn: () =>
      AgentsService.listAgentStatuses({
        workspaceId: workspaceFilter || undefined,
      }),
    staleTime: 30_000,
  })
  const statusByAgentId = new Map(
    (statusesData?.items ?? []).map((s) => [s.agent_id, s]),
  )

  // Keep the batched statuses fresh by invalidating on AGENT_STATUS_UPDATED events.
  useEffect(() => {
    const subId = eventService.subscribe(EventTypes.AGENT_STATUS_UPDATED, () => {
      queryClient.invalidateQueries({ queryKey: ["agentStatuses"] })
    })
    return () => {
      eventService.unsubscribe(subId)
    }
  }, [queryClient])

  if (agentsLoading) {
    return <PendingItems />
  }

  if (agentsError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading agents: {(agentsError as Error).message}
        </p>
      </div>
    )
  }

  const agents = data?.data || []

  if (agents.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Bot className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">
          You don't have any agents yet
        </h3>
        <p className="text-muted-foreground">Add a new agent to get started</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
      {agents.map((agent) => (
        <AgentCard
          key={agent.id}
          agent={agent}
          status={statusByAgentId.get(agent.id) ?? null}
        />
      ))}
    </div>
  )
}

function Agents() {
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()
  const { isDeveloper, isAgentUser } = useRole()

  // Phase 3 — agent-user view: same grid, but the page is "Installed
  // agents" rather than "Create and manage", and the AddAgent CTA is
  // hidden (creation is developer-only).
  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">
            {isAgentUser ? "Installed Agents" : "Agents"}
          </h1>
          <p className="text-xs text-muted-foreground">
            {isAgentUser
              ? "Agents you have installed from the catalog"
              : "Create and manage your agents"}
          </p>
        </div>
        {isDeveloper && <AddAgent />}
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, isAgentUser, isDeveloper])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <AgentsGrid key={activeWorkspaceId ?? 'default'} />
      </div>
    </div>
  )
}
