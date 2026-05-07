import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, EllipsisVertical, Sparkles } from "lucide-react"
import { useState, useEffect } from "react"

import { AgentsService } from "@/client"
import useRole from "@/hooks/useRole"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { AgentConfigTab } from "@/components/Agents/AgentConfigTab"
import { AgentIntegrationsTab } from "@/components/Agents/AgentIntegrationsTab"
import { AgentCredentialsTab } from "@/components/Agents/AgentCredentialsTab"
import { AgentPluginsTab } from "@/components/Agents/AgentPluginsTab"
import { AgentEnvironmentsTab } from "@/components/Agents/AgentEnvironmentsTab"
import { AgentInterfaceTab } from "@/components/Agents/AgentInterfaceTab"
import { AgentBundleTab } from "@/components/Agents/AgentBundleTab"
import { UpdateAvailableBanner } from "@/components/Agents/UpdateAvailableBanner"
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
  const { isDeveloper, isAgentUser } = useRole()
  const [menuOpen, setMenuOpen] = useState(false)

  const {
    data: agent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.readAgent({ id: agentId }),
    enabled: !!agentId,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    staleTime: 0,
  })

  const handleDeleteSuccess = () => {
    navigate({ to: "/agents" })
  }

  const { goBack } = useNavigationHistory()

  const handleBack = () => {
    goBack("/agents")
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
                {agent.is_general_assistant && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-violet-100 dark:bg-violet-900/40 px-2 py-0.5 text-xs font-medium text-violet-700 dark:text-violet-300 shrink-0">
                    <Sparkles className="h-3 w-3" />
                    General Assistant
                  </span>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {agent.is_publisher_install
                  ? "Publisher install"
                  : agent.bundle_uuid
                  ? "Bundle install"
                  : "Agent Configuration"}
              </p>
            </div>
          </div>
          {!agent.is_general_assistant && isDeveloper && (
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
          )}
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [agent, setHeaderContent, menuOpen, isDeveloper])

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

  const allTabs = [
    { value: "configuration", title: "Configuration", content: <AgentConfigTab agent={agent} /> },
    { value: "integrations", title: "Integrations", content: <AgentIntegrationsTab agent={agent} /> },
    { value: "credentials", title: "Credentials", content: <AgentCredentialsTab agentId={agent.id} /> },
    { value: "plugins", title: "Plugins", content: <AgentPluginsTab agentId={agent.id} /> },
    { value: "environments", title: "Environments", content: <AgentEnvironmentsTab agentId={agent.id} /> },
    { value: "interface", title: "Interface", content: <AgentInterfaceTab agent={agent} /> },
    { value: "bundle", title: "Bundle", content: <AgentBundleTab agent={agent} /> },
  ]

  // Phase 3 — agent-user view is conversation-only.  We keep
  // ``credentials`` (so users can fill in placeholder credentials for
  // their install) and ``environments`` (the install→chat entry point —
  // that tab surfaces the active env and its sessions).  All other
  // developer-tier tabs (configuration / integrations / plugins /
  // interface / bundle) stay hidden; any developer-only sub-controls
  // inside the kept tabs are gated separately by `isDeveloper`.
  let tabs = allTabs
  if (isAgentUser) {
    tabs = allTabs.filter(
      (tab) => tab.value === "environments" || tab.value === "credentials",
    )
  } else if (agent.is_general_assistant) {
    // Hide bundle tab for General Assistant agents (cannot be published).
    tabs = allTabs.filter((tab) => tab.value !== "bundle")
  }

  // Default tab depends on the visible tab set: agent-users land on
  // "environments" (their session entry point), developers land on
  // "configuration" (today's behavior).
  const defaultTab = isAgentUser ? "environments" : "configuration"

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        {/* Update banner for installs with pending updates */}
        <UpdateAvailableBanner agent={agent} />

        <HashTabs tabs={tabs} defaultTab={defaultTab} />
      </div>
    </div>
  )
}
