import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, EllipsisVertical, Share2 } from "lucide-react"
import { useState, useEffect } from "react"

import { AgentsService } from "@/client"
import { AgentPromptsTab } from "@/components/Agents/AgentPromptsTab"
import { AgentIntegrationsTab } from "@/components/Agents/AgentIntegrationsTab"
import { AgentCredentialsTab } from "@/components/Agents/AgentCredentialsTab"
import { AgentPluginsTab } from "@/components/Agents/AgentPluginsTab"
import { AgentEnvironmentsTab } from "@/components/Agents/AgentEnvironmentsTab"
import { AgentInterfaceTab } from "@/components/Agents/AgentInterfaceTab"
import { AgentSharingTab } from "@/components/Agents/AgentSharingTab"
import { UpdateBanner, ApplyUpdateDialog } from "@/components/Agents/CloneManagement"
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
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)
  const [applyUpdateDialogOpen, setApplyUpdateDialogOpen] = useState(false)

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
              <div className="flex items-center gap-2">
                <h1 className="text-base font-semibold truncate">{agent.name}</h1>
                {agent.is_clone && (
                  <Share2 className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {agent.is_clone && agent.shared_by_email
                  ? `Shared by ${agent.shared_by_email}`
                  : "Agent Configuration"
                }
              </p>
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

  // User mode clones only get limited tabs: Interface, Configuration (read-only), Clone Settings
  const isUserModeClone = agent.is_clone && agent.clone_mode === "user"

  const allTabs = [
    { value: "configuration", title: "Configuration", content: <AgentPromptsTab agent={agent} readOnly={isUserModeClone} /> },
    { value: "integrations", title: "Integrations", content: <AgentIntegrationsTab agent={agent} /> },
    { value: "credentials", title: "Credentials", content: <AgentCredentialsTab agentId={agent.id} /> },
    { value: "plugins", title: "Plugins", content: <AgentPluginsTab agentId={agent.id} /> },
    { value: "environments", title: "Environments", content: <AgentEnvironmentsTab agentId={agent.id} /> },
    { value: "interface", title: "Interface", content: <AgentInterfaceTab agent={agent} /> },
    { value: "sharing", title: agent.is_clone ? "Clone Settings" : "Sharing", content: <AgentSharingTab agent={agent} /> },
  ]

  // Filter tabs for user mode clones - show interface, configuration, environments, and clone settings
  // Environments is allowed since it doesn't expose credential values
  const tabs = isUserModeClone
    ? allTabs.filter(tab => ["interface", "configuration", "environments", "sharing"].includes(tab.value))
    : allTabs

  const handleUpdateApplied = () => {
    queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        {/* Update banner for clones with pending updates */}
        {agent.is_clone && agent.pending_update && (
          <UpdateBanner
            onApply={() => setApplyUpdateDialogOpen(true)}
            isLoading={false}
          />
        )}

        <HashTabs tabs={tabs} defaultTab="configuration" />

        {/* Apply update dialog */}
        <ApplyUpdateDialog
          open={applyUpdateDialogOpen}
          onOpenChange={setApplyUpdateDialogOpen}
          agentId={agent.id}
          onApplied={handleUpdateApplied}
        />
      </div>
    </div>
  )
}
