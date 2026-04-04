import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import { useEffect } from "react"
import { Network } from "lucide-react"
import { AgenticTeamsService } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import PendingItems from "@/components/Pending/PendingItems"
import { AgenticTeamSettings } from "@/components/AgenticTeams/AgenticTeamSettings"

export const Route = createFileRoute("/_layout/agentic-teams/")({
  component: AgenticTeamsIndexPage,
  head: () => ({
    meta: [
      {
        title: "Agentic Teams - Workflow Runner",
      },
    ],
  }),
})

function AgenticTeamsIndexPage() {
  const navigate = useNavigate()
  const { setHeaderContent } = usePageHeader()

  const { data: teamsData, isLoading } = useQuery({
    queryKey: ["agenticTeams"],
    queryFn: () => AgenticTeamsService.listAgenticTeams(),
  })

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center gap-2 min-w-0">
        <Network className="h-4 w-4 text-muted-foreground shrink-0" />
        <span className="font-medium truncate">Agentic Teams</span>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // If teams exist and user hasn't explicitly landed here, redirect to first team
  useEffect(() => {
    if (!isLoading && teamsData && teamsData.data.length > 0) {
      navigate({
        to: "/agentic-teams/$teamId",
        params: { teamId: teamsData.data[0].id },
      })
    }
  }, [isLoading, teamsData, navigate])

  if (isLoading) {
    return <PendingItems />
  }

  // Show management UI when there are no teams, or while redirect is pending
  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <AgenticTeamSettings />
      </div>
    </div>
  )
}
