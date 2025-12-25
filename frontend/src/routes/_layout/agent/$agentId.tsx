import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, EllipsisVertical } from "lucide-react"
import { useState, useEffect } from "react"

import { AgentsService } from "@/client"
import { AgentPromptsTab } from "@/components/Agents/AgentPromptsTab"
import { AgentCredentialsTab } from "@/components/Agents/AgentCredentialsTab"
import { AgentEnvironmentsTab } from "@/components/Agents/AgentEnvironmentsTab"
import EditAgent from "@/components/Agents/EditAgent"
import DeleteAgent from "@/components/Agents/DeleteAgent"
import PendingItems from "@/components/Pending/PendingItems"
import { HashTabs } from "@/components/Common/HashTabs"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/agent/$agentId")({
  component: AgentDetail,
})

function AgentDetail() {
  const { agentId } = Route.useParams()
  const navigate = useNavigate()
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)

  const {
    data: agent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.readAgent({ id: agentId }),
    enabled: !!agentId,
    refetchOnMount: "always", // Always refetch when component mounts to get latest prompts
    refetchOnWindowFocus: true, // Refetch when user returns to the window
    staleTime: 0, // Consider data stale immediately to ensure fresh data
  })

  const handleDeleteSuccess = () => {
    navigate({ to: "/agents" })
  }

  const handleBack = () => {
    navigate({ to: "/agents" })
  }

  // Update header when agent loads
  useEffect(() => {
    if (agent) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">{agent.name}</h1>
              <p className="text-xs text-muted-foreground">Agent Configuration</p>
            </div>
          </div>
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="shrink-0">
                <EllipsisVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <EditAgent agent={agent} onSuccess={() => setMenuOpen(false)} />
              <DeleteAgent
                id={agent.id}
                onSuccess={handleDeleteSuccess}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [agent, setHeaderContent, menuOpen])

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !agent) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading agent details</p>
      </div>
    )
  }

  const tabs = [
    { value: "prompts", title: "Prompts", content: <AgentPromptsTab agent={agent} /> },
    { value: "credentials", title: "Credentials", content: <AgentCredentialsTab agentId={agent.id} /> },
    { value: "environments", title: "Environments", content: <AgentEnvironmentsTab agentId={agent.id} /> },
  ]

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <HashTabs tabs={tabs} defaultTab="prompts" />
      </div>
    </div>
  )
}
