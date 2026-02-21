import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import { SessionsService, AgentsService } from "@/client"
import { AgentSessionsTable } from "@/components/Sessions/AgentSessionsTable"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/sessions/agent/$agentId")({
  component: AgentSessionsPage,
})

function AgentSessionsPage() {
  const { agentId } = Route.useParams()
  const { setHeaderContent } = usePageHeader()

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
    queryKey: ["sessions", "agent", agentId],
    queryFn: () =>
      SessionsService.listSessions({
        agentId,
        limit: 500,
        orderBy: "last_message_at",
        orderDesc: true,
      }),
  })

  const agent = agentData

  useEffect(() => {
    if (agent) {
      setHeaderContent(
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">{agent.name}</h1>
          <p className="text-xs text-muted-foreground">All sessions</p>
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

  const sessions = sessionsData?.data || []

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-6xl">
        <AgentSessionsTable sessions={sessions} />
      </div>
    </div>
  )
}
