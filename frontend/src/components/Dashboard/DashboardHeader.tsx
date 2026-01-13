import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Bot, Key } from "lucide-react"

import { AgentsService, CredentialsService } from "@/client"
import useWorkspace from "@/hooks/useWorkspace"
import { getColorPreset } from "@/utils/colorPresets"

export function DashboardHeader() {
  const { activeWorkspace, activeWorkspaceId } = useWorkspace()

  // Fetch agents for badge display
  const { data: agentsData } = useQuery({
    queryKey: ["agents", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
      })
    },
  })

  // Fetch credentials for badge display
  const { data: credentialsData } = useQuery({
    queryKey: ["credentials", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
      })
    },
  })

  const agents = agentsData?.data || []
  const credentials = credentialsData?.data || []

  const MAX_VISIBLE = 3
  const visibleAgents = agents.slice(0, MAX_VISIBLE)
  const extraAgents = agents.length - MAX_VISIBLE
  const visibleCredentials = credentials.slice(0, MAX_VISIBLE)
  const extraCredentials = credentials.length - MAX_VISIBLE

  // Get workspace name - show "Dashboard" for default workspace
  const workspaceName = activeWorkspaceId === null
    ? "Dashboard"
    : activeWorkspace && activeWorkspace !== "default" ? activeWorkspace.name : "Dashboard"

  const hasBadges = agents.length > 0 || credentials.length > 0

  return (
    <div className="min-w-0">
      <h1 className="text-lg font-semibold truncate">{workspaceName}</h1>
      {hasBadges ? (
        <div className="flex gap-1.5 mt-1 overflow-hidden">
          {/* Agent badges */}
          {visibleAgents.map((agent) => {
            const colorPreset = getColorPreset(agent.ui_color_preset)
            return (
              <Link
                key={agent.id}
                to="/agent/$agentId"
                params={{ agentId: agent.id }}
                className={`
                  inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-pointer whitespace-nowrap
                  opacity-50 transition-opacity hover:opacity-100
                  ${colorPreset.badgeBg} ${colorPreset.badgeText} ${colorPreset.badgeHover}
                `}
              >
                <Bot className="h-3 w-3" />
                {agent.name}
              </Link>
            )
          })}
          {extraAgents > 0 && (
            <Link
              to="/agents"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-pointer whitespace-nowrap opacity-50 transition-opacity hover:opacity-100 bg-gradient-to-r from-slate-100 to-slate-200 dark:from-slate-700 dark:to-slate-600 text-slate-500 dark:text-slate-300 hover:from-slate-200 hover:to-slate-300 dark:hover:from-slate-600 dark:hover:to-slate-500"
            >
              <Bot className="h-3 w-3" />
              +{extraAgents}
            </Link>
          )}
          {/* Credential badges */}
          {visibleCredentials.map((credential) => (
            <Link
              key={credential.id}
              to="/credential/$credentialId"
              params={{ credentialId: credential.id }}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-pointer whitespace-nowrap opacity-50 transition-opacity hover:opacity-100 bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700"
            >
              <Key className="h-3 w-3" />
              {credential.name}
            </Link>
          ))}
          {extraCredentials > 0 && (
            <Link
              to="/credentials"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-pointer whitespace-nowrap opacity-50 transition-opacity hover:opacity-100 bg-gradient-to-r from-slate-100 to-slate-200 dark:from-slate-700 dark:to-slate-600 text-slate-500 dark:text-slate-300 hover:from-slate-200 hover:to-slate-300 dark:hover:from-slate-600 dark:hover:to-slate-500"
            >
              <Key className="h-3 w-3" />
              +{extraCredentials}
            </Link>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">Start a new conversation with your agent</p>
      )}
    </div>
  )
}
