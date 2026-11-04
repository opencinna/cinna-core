import { useState, useRef, useCallback } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Globe, MessageSquare, ClipboardList, FileText, LayoutGrid, MoreVertical, Pencil, Trash2, Zap } from "lucide-react"

import type { UserDashboardBlockPublic, AgentPublic } from "@/client"
import { DashboardsService } from "@/client"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { componentTitle } from "@/components/Dashboard/AgentComponents/componentSpec"
import { focusTile, useIsTileFocused } from "@/components/Dashboard/AgentComponents/dashboardFocus"
import { readTileConfig } from "@/components/Dashboard/AgentComponents/tileConfig"
import { useAgentComponent } from "@/components/Dashboard/AgentComponents/useAgentComponent"
import { cn } from "@/lib/utils"
import { getColorPreset } from "@/utils/colorPresets"
import useCustomToast from "@/hooks/useCustomToast"

import { WebAppView } from "./views/WebAppView"
import { LatestSessionView } from "./views/LatestSessionView"
import { LatestTasksView } from "./views/LatestTasksView"
import { AgentEnvFileView } from "./views/AgentEnvFileView"
import { AgentComponentTile, agentComponentTileTitle } from "./views/AgentComponentView"
import { EditBlockDialog } from "./EditBlockDialog"
import { EditPromptActionsDialog } from "./EditPromptActionsDialog"
import { PromptActionsOverlay } from "./PromptActionsOverlay"

interface DashboardBlockProps {
  block: UserDashboardBlockPublic
  agent: AgentPublic | undefined
  dashboardId: string
  isEditMode: boolean
}

const VIEW_TYPE_ICONS: Record<string, React.ElementType> = {
  webapp: Globe,
  latest_session: MessageSquare,
  latest_tasks: ClipboardList,
  agent_env_file: FileText,
  agent_component: LayoutGrid,
}

export function DashboardBlock({ block, agent, dashboardId, isEditMode }: DashboardBlockProps) {
  const queryClient = useQueryClient()
  const [showEditDialog, setShowEditDialog] = useState(false)
  const [showPromptActionsDialog, setShowPromptActionsDialog] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [isHovered, setIsHovered] = useState(false)

  // Ref to the webapp iframe — only used when view_type === "webapp".
  // Passed to WebAppView (to attach to the <iframe>) and to PromptActionsOverlay
  // (to collect page context via postMessage before creating a new session).
  const webappIframeRef = useRef<HTMLIFrameElement | null>(null)

  const { showErrorToast } = useCustomToast()
  const colorPreset = getColorPreset(agent?.ui_color_preset)
  const ViewIcon = VIEW_TYPE_ICONS[block.view_type] ?? MessageSquare
  const isComponent = block.view_type === "agent_component"
  const componentConfig = isComponent ? readTileConfig(block) : null
  // Shares its query with the tile body: one request per component.
  const componentQuery = useAgentComponent(
    agent?.id,
    componentConfig?.component ?? "",
    componentConfig?.skill ?? null,
    isComponent,
  )
  const title = isComponent
    ? agentComponentTileTitle(block, componentTitle(componentQuery.data))
    : block.title || agent?.name || "Unknown Agent"
  const isFocused = useIsTileFocused(dashboardId, block.id)
  const focusThisTile = isComponent ? () => focusTile(dashboardId, block.id) : undefined

  const handleStreamComplete = useCallback(() => {
    if (!agent) return
    switch (block.view_type) {
      case "latest_session":
        queryClient.invalidateQueries({ queryKey: ["dashboardBlockSessions", agent.id] })
        break
      case "latest_tasks":
        queryClient.invalidateQueries({ queryKey: ["dashboardBlockTasks", agent.id] })
        break
      case "agent_env_file":
        queryClient.invalidateQueries({ queryKey: ["dashboardBlockEnvFile", dashboardId, block.id, (block.config as Record<string, string> | null)?.file_path ?? ""] })
        break
    }
  }, [agent, block.view_type, block.id, block.config, dashboardId, queryClient])

  const deleteBlockMutation = useMutation({
    mutationFn: () =>
      DashboardsService.deleteBlock({
        dashboardId,
        blockId: block.id,
      }),
    onSuccess: () => {
      setShowDeleteConfirm(false)
      queryClient.invalidateQueries({ queryKey: ["userDashboard", dashboardId] })
    },
    onError: () => {
      showErrorToast("Failed to remove block. Please try again.")
    },
  })

  const renderView = () => {
    if (!agent) {
      return (
        <div className="flex items-center justify-center h-full">
          <p className="text-sm text-muted-foreground">Agent unavailable</p>
        </div>
      )
    }

    switch (block.view_type) {
      case "webapp":
        return (
          <WebAppView
            agentId={agent.id}
            webappEnabled={agent.webapp_enabled ?? false}
            iframeRef={webappIframeRef}
          />
        )
      case "latest_tasks":
        return <LatestTasksView agentId={agent.id} />
      case "agent_env_file":
        return (
          <AgentEnvFileView
            dashboardId={dashboardId}
            blockId={block.id}
            filePath={(block.config as Record<string, string> | null)?.file_path ?? ""}
          />
        )
      case "agent_component":
        return (
          <AgentComponentTile
            block={block}
            agent={agent}
            dashboardId={dashboardId}
            title={title}
          />
        )
      default:
        return <LatestSessionView agentId={agent.id} />
    }
  }

  return (
    <div
      className={cn(
        "flex flex-col h-full bg-background overflow-hidden rounded-lg",
        isEditMode && "border border-border",
        !isEditMode && block.show_border && "border border-border",
        // The tile the dashboard chat is about (exactly one per page).
        isComponent && isFocused && "ring-2 ring-primary ring-offset-1",
      )}
    >
      {/* Block header - visible in edit mode or when show_header is enabled */}
      {(isEditMode || block.show_header) && (
        // biome-ignore lint/a11y/useKeyWithClickEvents: a click on the header focuses the tile for the dashboard chat; the tile's Ask button is the keyboard route.
        // biome-ignore lint/a11y/noStaticElementInteractions: as above.
        <div
          className={cn("flex items-center gap-2 px-3 py-2 border-b shrink-0", colorPreset.badgeBg)}
          onClick={focusThisTile}
        >
          <span
            className={cn("h-2 w-2 rounded-full shrink-0", colorPreset.badgeText, "bg-current")}
          />
          <ViewIcon className={cn("h-3.5 w-3.5 shrink-0", colorPreset.badgeText)} />
          <span className={cn("text-xs font-medium flex-1 truncate", colorPreset.badgeText)}>
            {title}
          </span>
          {isEditMode && (
            <DropdownMenu>
              <Tooltip>
                <TooltipTrigger asChild>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5 shrink-0"
                      aria-label={`Block actions for ${title}`}
                    >
                      <MoreVertical className="h-3.5 w-3.5" />
                    </Button>
                  </DropdownMenuTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs">
                  Block actions
                </TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setShowEditDialog(true)}>
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit Block
                </DropdownMenuItem>
                {/* A component's questions live in the dashboard chat (D-C5). */}
                {!isComponent && (
                  <DropdownMenuItem onClick={() => setShowPromptActionsDialog(true)}>
                    <Zap className="mr-2 h-4 w-4" />
                    Edit Prompt Actions
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onClick={() => setShowDeleteConfirm(true)}
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Remove Block
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}

      {/* Content area with prompt actions overlay */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: hover only reveals the prompt-action overlay, whose buttons are reachable by keyboard. */}
      <div
        className="flex-1 overflow-hidden min-h-0 relative"
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
      >
        {renderView()}
        {!isEditMode && !isComponent && agent && block.prompt_actions && block.prompt_actions.length > 0 && (
          <PromptActionsOverlay
            actions={block.prompt_actions}
            agentId={agent.id}
            blockId={block.id}
            dashboardId={dashboardId}
            isVisible={isHovered}
            isWebApp={block.view_type === "webapp"}
            iframeRef={block.view_type === "webapp" ? webappIframeRef : undefined}
            onStreamComplete={handleStreamComplete}
          />
        )}
      </div>

      {/* Owned by the block, not the menu item: a menu item unmounts on select. */}
      <AlertDialog
        open={showDeleteConfirm}
        onOpenChange={(open) => {
          if (!deleteBlockMutation.isPending) setShowDeleteConfirm(open)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">Remove block {title}?</AlertDialogTitle>
            <AlertDialogDescription>
              It leaves this dashboard. The agent and its data stay as they are.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteBlockMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteBlockMutation.isPending}
              onClick={(event) => {
                event.preventDefault()
                deleteBlockMutation.mutate()
              }}
            >
              {deleteBlockMutation.isPending ? "Removing…" : "Remove block"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {showEditDialog && (
        <EditBlockDialog
          block={block}
          dashboardId={dashboardId}
          open={showEditDialog}
          onOpenChange={setShowEditDialog}
        />
      )}

      {showPromptActionsDialog && (
        <EditPromptActionsDialog
          block={block}
          dashboardId={dashboardId}
          open={showPromptActionsDialog}
          onOpenChange={setShowPromptActionsDialog}
        />
      )}
    </div>
  )
}
