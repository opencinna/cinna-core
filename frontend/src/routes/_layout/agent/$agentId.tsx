import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, EllipsisVertical, Package, Sparkles, Tag, User } from "lucide-react"
import { useState, useEffect } from "react"

import { AgentsService, BundlesService } from "@/client"
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
import { SetupNeededBanner } from "@/components/Install/SetupNeededBanner"
import EditAgent from "@/components/Agents/EditAgent"
import DeleteAgent from "@/components/Agents/DeleteAgent"
import UninstallAgent from "@/components/Agents/UninstallAgent"
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

  const isForeignInstallHeader = !!agent?.bundle_uuid && !agent?.is_publisher_install
  const { data: bundle } = useQuery({
    queryKey: ["bundles", agent?.bundle_uuid],
    queryFn: () =>
      BundlesService.getBundle({ bundleUuid: agent?.bundle_uuid as string }),
    enabled: isForeignInstallHeader,
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
              {agent.bundle_uuid && !agent.is_publisher_install ? (
                <div className="flex gap-1.5 mt-1 overflow-hidden">
                  <span
                    title={`Bundle ID: ${agent.bundle_id}`}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs whitespace-nowrap opacity-50 hover:opacity-100 transition-opacity bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"
                  >
                    <Package className="h-3 w-3" />
                    {agent.bundle_id}
                  </span>
                  {agent.installed_revision_number != null && (
                    <span
                      title="Installed revision"
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs whitespace-nowrap opacity-50 hover:opacity-100 transition-opacity bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"
                    >
                      <Tag className="h-3 w-3" />
                      v{agent.installed_revision_number}
                    </span>
                  )}
                  {(bundle?.publisher_name || bundle?.publisher_email || bundle?.publisher_handle) && (
                    <span
                      title="Bundle author"
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs whitespace-nowrap opacity-50 hover:opacity-100 transition-opacity bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"
                    >
                      <User className="h-3 w-3" />
                      {bundle.publisher_name || bundle.publisher_email || bundle.publisher_handle}
                    </span>
                  )}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">
                  {agent.is_publisher_install
                    ? "Publisher install"
                    : "Agent Configuration"}
                </p>
              )}
            </div>
          </div>
          {(() => {
            const isForeignInstallHdr =
              !!agent.bundle_uuid && !agent.is_publisher_install
            const showDeveloperActions =
              !agent.is_general_assistant && isDeveloper
            const showUninstall = isForeignInstallHdr
            if (!showDeveloperActions && !showUninstall) return null
            return (
              <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="sm" className="shrink-0">
                    <EllipsisVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {showDeveloperActions && (
                    <>
                      <EditAgent
                        agent={agent}
                        onSuccess={() => setMenuOpen(false)}
                      />
                      <DeleteAgent
                        id={agent.id}
                        onSuccess={handleDeleteSuccess}
                      />
                    </>
                  )}
                  {showUninstall && (
                    <UninstallAgent
                      agentId={agent.id}
                      onSuccess={handleDeleteSuccess}
                    />
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )
          })()}
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [agent, bundle, setHeaderContent, menuOpen, isDeveloper])

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

  // Foreign install = install of a bundle published by someone else.
  // The current user owns the install row, but the bundle content
  // (prompts, description, etc.) was authored by the publisher, so the
  // configuration tab must render read-only for these installs.
  const isForeignInstall = !!agent.bundle_uuid && !agent.is_publisher_install
  const configReadOnly = isForeignInstall

  const allTabs = [
    {
      value: "configuration",
      title: "Configuration",
      content: (
        <AgentConfigTab
          agent={agent}
          readOnly={configReadOnly}
          // Agent-users see the simplified view (just Information +
          // Agent Prompts). Schedules / Handovers belong to the
          // developer-tier configuration surface.
          showOperationalSettings={!isAgentUser}
        />
      ),
    },
    { value: "integrations", title: "Integrations", content: <AgentIntegrationsTab agent={agent} /> },
    { value: "credentials", title: "Credentials", content: <AgentCredentialsTab agentId={agent.id} /> },
    { value: "plugins", title: "Plugins", content: <AgentPluginsTab agentId={agent.id} /> },
    { value: "environments", title: "Environments", content: <AgentEnvironmentsTab agentId={agent.id} /> },
    { value: "interface", title: "Interface", content: <AgentInterfaceTab agent={agent} /> },
    { value: "bundle", title: "Bundle", content: <AgentBundleTab agent={agent} /> },
  ]

  // Phase 3 — agent-user view is conversation-only with one read-only
  // peek into the agent's identity.  Visible tabs:
  //   - ``configuration`` (read-only, Information + Agent Prompts only)
  //   - ``credentials``   (fill in placeholder credentials)
  //   - ``environments``  (install→chat entry point, active sessions)
  // All other developer-tier tabs (integrations / plugins / interface /
  // bundle) stay hidden; developer-only sub-controls inside the kept
  // tabs are gated separately by `isDeveloper`.
  let tabs = allTabs
  if (isAgentUser) {
    // Agent-users see the integrations tab too, but inside it only the
    // MCP Connectors card is rendered (see ``AgentIntegrationsTab`` — it
    // reads ``useRole`` and degrades to a single card for agent-users).
    const agentUserTabs = new Set([
      "configuration",
      "credentials",
      "environments",
      "integrations",
    ])
    tabs = allTabs.filter((tab) => agentUserTabs.has(tab.value))
  } else if (agent.is_general_assistant) {
    // Hide bundle tab for General Assistant agents (cannot be published).
    tabs = allTabs.filter((tab) => tab.value !== "bundle")
  }

  // Default tab: everyone lands on "configuration" — for developers
  // that's the editable config surface, for agent-users it's the
  // read-only Information + Agent Prompts peek.
  const defaultTab = "configuration"

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        {/* Setup-needed banner (Phase 4 pre-LLM gate). Renders nothing when
            the install is ready. Shown above the chat/tabs surface so it's
            the first thing users see when an install needs attention. */}
        <SetupNeededBanner agentId={agent.id} />

        {/* Update banner for installs with pending updates */}
        <UpdateAvailableBanner agent={agent} />

        <HashTabs tabs={tabs} defaultTab={defaultTab} />
      </div>
    </div>
  )
}
