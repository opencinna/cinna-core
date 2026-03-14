import { useState, useRef, useCallback } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Globe, MessageSquare, ClipboardList, FileText, MoreVertical, Pencil, Trash2, Zap } from "lucide-react"

import type { UserDashboardBlockPublic, AgentPublic } from "@/client"
import { DashboardsService } from "@/client"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { getColorPreset } from "@/utils/colorPresets"
import useCustomToast from "@/hooks/useCustomToast"

import { WebAppView } from "./views/WebAppView"
import { LatestSessionView } from "./views/LatestSessionView"
import { LatestTasksView } from "./views/LatestTasksView"
import { AgentEnvFileView } from "./views/AgentEnvFileView"
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
  const title = block.title || agent?.name || "Unknown Agent"

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
      case "latest_session":
      default:
        return <LatestSessionView agentId={agent.id} />
    }
  }

  return (
    <div
      className={cn(
        "flex flex-col h-full bg-background overflow-hidden rounded-lg",
        isEditMode && "border border-border",
        !isEditMode && block.show_border && "border border-border"
      )}
    >
      {/* Block header - visible in edit mode or when show_header is enabled */}
      {(isEditMode || block.show_header) && (
        <div className={cn("flex items-center gap-2 px-3 py-2 border-b shrink-0", colorPreset.badgeBg)}>
          <span
            className={cn("h-2 w-2 rounded-full shrink-0", colorPreset.badgeText, "bg-current")}
          />
          <ViewIcon className={cn("h-3.5 w-3.5 shrink-0", colorPreset.badgeText)} />
          <span className={cn("text-xs font-medium flex-1 truncate", colorPreset.badgeText)}>
            {title}
          </span>
          {isEditMode && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" className="h-5 w-5 shrink-0">
                  <MoreVertical className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setShowEditDialog(true)}>
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit Block
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setShowPromptActionsDialog(true)}>
                  <Zap className="mr-2 h-4 w-4" />
                  Edit Prompt Actions
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => setShowDeleteConfirm(true)}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Remove Block
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}

      {/* Delete confirmation inline */}
      {showDeleteConfirm ? (
        <div className="flex flex-col items-center justify-center h-full p-4 gap-3 text-center">
          <p className="text-sm font-medium">Remove this block?</p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="destructive"
              className="h-7 text-xs"
              onClick={() => deleteBlockMutation.mutate()}
              disabled={deleteBlockMutation.isPending}
            >
              Remove
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              onClick={() => setShowDeleteConfirm(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        /* Content area with prompt actions overlay */
        <div
          className="flex-1 overflow-hidden min-h-0 relative"
          onMouseEnter={() => setIsHovered(true)}
          onMouseLeave={() => setIsHovered(false)}
        >
          {renderView()}
          {!isEditMode && agent && block.prompt_actions && block.prompt_actions.length > 0 && (
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
      )}

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
