import { FolderKanban, Check, Settings } from "lucide-react"
import { useNavigate } from "@tanstack/react-router"

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
import useWorkspace from "@/hooks/useWorkspace"
import { getWorkspaceIcon } from "@/config/workspaceIcons"
import { cn } from "@/lib/utils"

export const SidebarWorkspaceSwitcher = () => {
  const { isMobile } = useSidebar()
  const navigate = useNavigate()
  const { workspaces, activeWorkspace, switchWorkspace, activeWorkspaceId } = useWorkspace()

  const activeWorkspaceName =
    activeWorkspace === "default" ? "Default" : activeWorkspace?.name || "Default"

  const activeWorkspaceIcon =
    activeWorkspace === "default" ? null : activeWorkspace?.icon || null

  const ActiveIcon = getWorkspaceIcon(activeWorkspaceIcon)

  const handleWorkspaceSelect = (workspaceId: string | null) => {
    switchWorkspace(workspaceId)
  }

  return (
    <>
      <SidebarMenuItem>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton tooltip="Workspace" className="h-10 text-base font-semibold" title="Workspace — an isolated space for organizing your agents, sessions, and credentials by context">
              <ActiveIcon className="size-5 text-muted-foreground" />
              <span className="truncate">{activeWorkspaceName}</span>
              <span className="sr-only">Switch workspace</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side={isMobile ? "bottom" : "right"}
            align="start"
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
          >
            {/* Default workspace */}
            <DropdownMenuItem
              onClick={() => handleWorkspaceSelect(null)}
              className={cn(
                "flex items-center justify-between",
                activeWorkspaceId === null && "bg-accent"
              )}
            >
              <div className="flex items-center">
                <FolderKanban className="mr-2 h-4 w-4" />
                Default
              </div>
              {activeWorkspaceId === null && <Check className="h-4 w-4" />}
            </DropdownMenuItem>

            {/* User workspaces */}
            {workspaces.map((workspace) => {
              const WorkspaceIcon = getWorkspaceIcon(workspace.icon)
              const isActive = activeWorkspaceId === workspace.id
              return (
                <DropdownMenuItem
                  key={workspace.id}
                  onClick={() => handleWorkspaceSelect(workspace.id)}
                  className={cn(
                    "flex items-center justify-between",
                    isActive && "bg-accent"
                  )}
                >
                  <div className="flex items-center">
                    <WorkspaceIcon className="mr-2 h-4 w-4" />
                    {workspace.name}
                  </div>
                  {isActive && <Check className="h-4 w-4" />}
                </DropdownMenuItem>
              )
            })}

            <DropdownMenuSeparator />

<DropdownMenuItem onClick={() => navigate({ to: "/settings", hash: "interface" })}>
              <Settings className="mr-2 h-4 w-4" />
              Manage Workspaces
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </>
  )
}
