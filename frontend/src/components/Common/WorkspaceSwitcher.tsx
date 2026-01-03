import { FolderKanban, Plus } from "lucide-react"
import { useState } from "react"

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
import { CreateWorkspaceModal } from "./CreateWorkspaceModal"

export const SidebarWorkspaceSwitcher = () => {
  const { isMobile } = useSidebar()
  const { workspaces, activeWorkspace, switchWorkspace } = useWorkspace()
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false)

  const activeWorkspaceName =
    activeWorkspace === "default" ? "Default" : activeWorkspace?.name || "Default"

  const handleWorkspaceSelect = (workspaceId: string | null) => {
    switchWorkspace(workspaceId)
  }

  const handleNewWorkspaceClick = () => {
    setIsCreateModalOpen(true)
  }

  return (
    <>
      <SidebarMenuItem>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton tooltip="Workspace">
              <FolderKanban className="size-4 text-muted-foreground" />
              <span>{activeWorkspaceName}</span>
              <span className="sr-only">Switch workspace</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side={isMobile ? "top" : "right"}
            align="end"
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
          >
            {/* Default workspace */}
            <DropdownMenuItem onClick={() => handleWorkspaceSelect(null)}>
              <FolderKanban className="mr-2 h-4 w-4" />
              Default
            </DropdownMenuItem>

            {/* User workspaces */}
            {workspaces.map((workspace) => (
              <DropdownMenuItem
                key={workspace.id}
                onClick={() => handleWorkspaceSelect(workspace.id)}
              >
                <FolderKanban className="mr-2 h-4 w-4" />
                {workspace.name}
              </DropdownMenuItem>
            ))}

            <DropdownMenuSeparator />

            {/* New workspace option */}
            <DropdownMenuItem onClick={handleNewWorkspaceClick}>
              <Plus className="mr-2 h-4 w-4" />
              New Workspace
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>

      <CreateWorkspaceModal
        open={isCreateModalOpen}
        onClose={() => setIsCreateModalOpen(false)}
      />
    </>
  )
}
