import { Check, Network, Settings } from "lucide-react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate, useRouterState } from "@tanstack/react-router"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { AgenticTeamsService } from "@/client"
import { cn } from "@/lib/utils"
import { getWorkspaceIcon } from "@/config/workspaceIcons"

export const AgenticTeamsSwitcher = () => {
  const { isMobile } = useSidebar()
  const navigate = useNavigate()
  const routerState = useRouterState()
  const { data: teamsData } = useQuery({
    queryKey: ["agenticTeams"],
    queryFn: () => AgenticTeamsService.listAgenticTeams(),
  })

  const teams = teamsData?.data ?? []

  // Determine active team from current route
  const pathname = routerState.location.pathname
  const agenticTeamsMatch = pathname.match(/^\/agentic-teams\/([^/]+)/)
  const activeTeamId = agenticTeamsMatch ? agenticTeamsMatch[1] : null
  const activeTeam = teams.find((t) => t.id === activeTeamId)

  const label = activeTeam?.name ?? "Agentic Teams"

  return (
    <>
      <SidebarMenuItem>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton tooltip="Agentic Teams">
              <Network className="size-4 text-muted-foreground" />
              <span>{label}</span>
              <span className="sr-only">Switch agentic team</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side={isMobile ? "top" : "right"}
            align="end"
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
          >
            {teams.length === 0 ? (
              <DropdownMenuItem disabled>
                <span className="text-muted-foreground">No teams yet</span>
              </DropdownMenuItem>
            ) : (
              teams.map((team) => {
                const isActive = activeTeamId === team.id
                const TeamIcon = getWorkspaceIcon(team.icon)
                return (
                  <DropdownMenuItem
                    key={team.id}
                    onClick={() =>
                      navigate({ to: "/agentic-teams/$teamId", params: { teamId: team.id } })
                    }
                    className={cn(
                      "flex items-center justify-between",
                      isActive && "bg-accent",
                    )}
                  >
                    <div className="flex items-center">
                      <TeamIcon className="mr-2 h-4 w-4" />
                      {team.name}
                    </div>
                    {isActive && <Check className="h-4 w-4" />}
                  </DropdownMenuItem>
                )
              })
            )}

            <DropdownMenuSeparator />

<DropdownMenuItem onClick={() => navigate({ to: "/settings", hash: "interface" })}>
              <Settings className="mr-2 h-4 w-4" />
              Manage Teams
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </>
  )
}
