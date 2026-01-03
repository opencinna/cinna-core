import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useMemo } from "react"

import { SessionsService } from "@/client"
import type { SessionPublicExtended } from "@/client"
import { AgentSessionsGroup } from "@/components/Sessions/AgentSessionsGroup"
import PendingItems from "@/components/Pending/PendingItems"
import useWorkspace from "@/hooks/useWorkspace"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/sessions")({
  component: SessionsList,
})

function SessionsList() {
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()

  const {
    data: sessionsData,
    isLoading: sessionsLoading,
    error: sessionsError,
  } = useQuery({
    queryKey: ["sessions", activeWorkspaceId],
    queryFn: () => SessionsService.listSessions({
      userWorkspaceId: activeWorkspaceId || undefined,
    }),
  })

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Sessions</h1>
        <p className="text-xs text-muted-foreground">Manage your conversation sessions</p>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Group sessions by agent
  const agentGroups = useMemo(() => {
    const sessions = sessionsData?.data || []
    const groups = new Map<string, {
      agentId: string
      agentName: string
      agentColorPreset: string | null
      sessions: SessionPublicExtended[]
    }>()

    sessions.forEach((session) => {
      const agentId = session.agent_id || "unknown"
      const agentName = session.agent_name || "Unknown Agent"

      if (!groups.has(agentId)) {
        groups.set(agentId, {
          agentId,
          agentName,
          agentColorPreset: session.agent_ui_color_preset,
          sessions: [],
        })
      }

      groups.get(agentId)!.sessions.push(session)
    })

    return Array.from(groups.values())
  }, [sessionsData])

  if (sessionsLoading) {
    return <PendingItems />
  }

  if (sessionsError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading sessions</p>
      </div>
    )
  }

  const sessions = sessionsData?.data || []

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-6">
        {/* Agent Sessions Grid */}
        {sessions.length === 0 ? (
          <div className="text-center py-12 border-2 border-dashed rounded-lg">
            <p className="text-muted-foreground">No sessions yet</p>
          </div>
        ) : (
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
            {agentGroups.map((group) => (
              <AgentSessionsGroup
                key={group.agentId}
                agentId={group.agentId}
                agentName={group.agentName}
                agentColorPreset={group.agentColorPreset}
                sessions={group.sessions}
                maxSessions={10}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
