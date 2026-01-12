import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { SessionsService, AgentsService } from "@/client"
import { AgentConversations } from "@/components/Sessions/AgentConversations"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import useWorkspace from "@/hooks/useWorkspace"

export const Route = createFileRoute("/_layout/agent/$agentId/conversations")({
  component: AgentConversationsPage,
})

function AgentConversationsPage() {
  const { agentId } = Route.useParams()
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()

  const {
    data: agentData,
    isLoading: agentLoading,
  } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.readAgent({ id: agentId }),
  })

  const {
    data: sessionsData,
    isLoading: sessionsLoading,
    error: sessionsError,
  } = useQuery({
    queryKey: ["sessions", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return SessionsService.listSessions({
        userWorkspaceId: workspaceId ?? "",
      })
    },
  })

  const agent = agentData

  // Filter sessions for this agent
  const agentSessions = useMemo(() => {
    const sessions = sessionsData?.data || []
    return sessions.filter((session) => session.agent_id === agentId)
  }, [sessionsData, agentId])

  useEffect(() => {
    if (agent) {
      setHeaderContent(
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">{agent.name}</h1>
          <p className="text-xs text-muted-foreground">All conversations</p>
        </div>
      )
    }
    return () => setHeaderContent(null)
  }, [setHeaderContent, agent])

  if (agentLoading || sessionsLoading) {
    return <PendingItems />
  }

  if (sessionsError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading sessions</p>
      </div>
    )
  }

  if (!agent) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Agent not found</p>
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-3xl">
        <AgentConversations
          sessions={agentSessions}
          agentName={agent.name}
          agentColorPreset={agent.ui_color_preset}
        />
      </div>
    </div>
  )
}
