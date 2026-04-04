import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import {
  Network,
  Lock,
  Unlock,
  EllipsisVertical,
  Pencil,
  Trash2,
  ClipboardList,
} from "lucide-react"

import { AgenticTeamsService } from "@/client"
import type { AgenticTeamNodePositionUpdate } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import PendingItems from "@/components/Pending/PendingItems"
import { AgenticTeamChart } from "@/components/AgenticTeams/AgenticTeamChart"
import { AgenticTeamFormDialog } from "@/components/AgenticTeams/AgenticTeamSettings"
import { TaskBoard } from "@/components/Tasks/TaskBoard"
import { Button } from "@/components/ui/button"
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
import { cn } from "@/lib/utils"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/agentic-teams/$teamId")({
  component: AgenticTeamChartPage,
  head: () => ({
    meta: [
      {
        title: "Agentic Team Chart - Workflow Runner",
      },
    ],
  }),
})

function AgenticTeamChartPage() {
  const { teamId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [isEditMode, setIsEditMode] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)
  const [activeTab, setActiveTab] = useState<"chart" | "tasks">("chart")

  const { data: chartData, isLoading } = useQuery({
    queryKey: ["agenticTeamChart", teamId],
    queryFn: () => AgenticTeamsService.getAgenticTeamChart({ teamId }),
  })

  // Team CRUD mutations
  const deleteTeamMutation = useMutation({
    mutationFn: () => AgenticTeamsService.deleteAgenticTeam({ teamId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeams"] })
      showSuccessToast("Team deleted")
      navigate({ to: "/agentic-teams" })
    },
    onError: () => showErrorToast("Failed to delete team"),
  })

  const updateTeamMutation = useMutation({
    mutationFn: (data: { name?: string; icon?: string; task_prefix?: string | null }) =>
      AgenticTeamsService.updateAgenticTeam({ teamId, requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeams"] })
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
      showSuccessToast("Team updated")
      setShowEditDialog(false)
    },
    onError: () => showErrorToast("Failed to update team"),
  })

  // Node mutations
  const createNodeMutation = useMutation({
    mutationFn: ({
      agentId,
      isLead,
    }: {
      agentId: string
      isLead: boolean
    }) =>
      AgenticTeamsService.createTeamNode({
        teamId,
        requestBody: { agent_id: agentId, is_lead: isLead },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to add node"),
  })

  const updateNodeMutation = useMutation({
    mutationFn: ({
      nodeId,
      updates,
    }: {
      nodeId: string
      updates: { is_lead?: boolean }
    }) =>
      AgenticTeamsService.updateTeamNode({
        teamId,
        nodeId,
        requestBody: updates,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to update node"),
  })

  const deleteNodeMutation = useMutation({
    mutationFn: (nodeId: string) =>
      AgenticTeamsService.deleteTeamNode({ teamId, nodeId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to delete node"),
  })

  const bulkUpdatePositionsMutation = useMutation({
    mutationFn: (positions: AgenticTeamNodePositionUpdate[]) =>
      AgenticTeamsService.bulkUpdateNodePositions({
        teamId,
        requestBody: positions,
      }),
    // No invalidation needed — positions are purely UI state
  })

  // Connection mutations
  const createConnectionMutation = useMutation({
    mutationFn: ({
      sourceId,
      targetId,
    }: {
      sourceId: string
      targetId: string
    }) =>
      AgenticTeamsService.createTeamConnection({
        teamId,
        requestBody: {
          source_node_id: sourceId,
          target_node_id: targetId,
          connection_prompt: "",
          enabled: true,
        },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to create connection"),
  })

  const updateConnectionMutation = useMutation({
    mutationFn: ({
      connId,
      prompt,
      enabled,
    }: {
      connId: string
      prompt: string
      enabled: boolean
    }) =>
      AgenticTeamsService.updateTeamConnection({
        teamId,
        connId,
        requestBody: { connection_prompt: prompt, enabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to update connection"),
  })

  const deleteConnectionMutation = useMutation({
    mutationFn: (connId: string) =>
      AgenticTeamsService.deleteTeamConnection({ teamId, connId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeamChart", teamId] })
    },
    onError: () => showErrorToast("Failed to delete connection"),
  })

  const [generatedPrompt, setGeneratedPrompt] = useState<string | null>(null)

  const generateConnectionPromptMutation = useMutation({
    mutationFn: (connId: string) =>
      AgenticTeamsService.generateConnectionPrompt({ teamId, connId }),
    onSuccess: (data) => {
      if (data.success && data.connection_prompt) {
        setGeneratedPrompt(data.connection_prompt)
        showSuccessToast("Prompt generated")
      } else {
        showErrorToast(data.error || "Failed to generate prompt")
      }
    },
    onError: () => showErrorToast("Failed to generate prompt"),
  })

  const team = chartData?.team

  useEffect(() => {
    if (team) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Network className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="font-medium truncate">{team.name}</span>
            {/* Tab navigation */}
            <div className="flex items-center border rounded-md overflow-hidden ml-2">
              <button
                onClick={() => setActiveTab("chart")}
                className={cn(
                  "px-2.5 py-1 text-xs flex items-center gap-1.5 transition-colors",
                  activeTab === "chart"
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-muted"
                )}
              >
                <Network className="h-3.5 w-3.5" />
                Chart
              </button>
              <button
                onClick={() => setActiveTab("tasks")}
                className={cn(
                  "px-2.5 py-1 text-xs flex items-center gap-1.5 transition-colors",
                  activeTab === "tasks"
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-muted"
                )}
              >
                <ClipboardList className="h-3.5 w-3.5" />
                Tasks
              </button>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {activeTab === "chart" && (
              <Button
                size="icon"
                variant={isEditMode ? "default" : "outline"}
                onClick={() => setIsEditMode((v) => !v)}
                className="h-8 w-8 group"
                title={isEditMode ? "Lock Layout" : "Edit Layout"}
              >
                {isEditMode ? (
                  <>
                    <Unlock className="h-4 w-4 group-hover:hidden" />
                    <Lock className="h-4 w-4 hidden group-hover:block" />
                  </>
                ) : (
                  <>
                    <Lock className="h-4 w-4 group-hover:hidden" />
                    <Unlock className="h-4 w-4 hidden group-hover:block" />
                  </>
                )}
              </Button>
            )}
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="shrink-0">
                  <EllipsisVertical className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() => {
                    setShowEditDialog(true)
                    setMenuOpen(false)
                  }}
                >
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit Team
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => {
                    setShowDeleteDialog(true)
                    setMenuOpen(false)
                  }}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Delete Team
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </>,
      )
    }
    return () => setHeaderContent(null)
  }, [team, setHeaderContent, isEditMode, menuOpen, activeTab])

  if (isLoading) {
    return <PendingItems />
  }

  if (!chartData || !team) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Team not found</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 relative">
        {activeTab === "chart" ? (
          <AgenticTeamChart
            teamId={teamId}
            nodes={chartData.nodes}
            connections={chartData.connections}
            isEditMode={isEditMode}
            onCreateNode={(agentId, isLead) =>
              createNodeMutation.mutate({ agentId, isLead })
            }
            onUpdateNode={(nodeId, updates) =>
              updateNodeMutation.mutate({ nodeId, updates })
            }
            onDeleteNode={(nodeId) => deleteNodeMutation.mutate(nodeId)}
            onBulkUpdatePositions={(positions) =>
              bulkUpdatePositionsMutation.mutate(positions)
            }
            onCreateConnection={(sourceId, targetId) =>
              createConnectionMutation.mutate({ sourceId, targetId })
            }
            onUpdateConnection={(connId, prompt, enabled) =>
              updateConnectionMutation.mutate({ connId, prompt, enabled })
            }
            onDeleteConnection={(connId) => deleteConnectionMutation.mutate(connId)}
            onGenerateConnectionPrompt={(connId) => {
              setGeneratedPrompt(null)
              generateConnectionPromptMutation.mutate(connId)
            }}
            isCreatingNode={createNodeMutation.isPending}
            isCreatingConnection={updateConnectionMutation.isPending}
            isGeneratingPrompt={generateConnectionPromptMutation.isPending}
            generatedPrompt={generatedPrompt}
          />
        ) : (
          <div className="p-6 overflow-y-auto h-full">
            <TaskBoard teamId={teamId} />
          </div>
        )}
      </div>

      {/* Edit Team Dialog */}
      <AgenticTeamFormDialog
        open={showEditDialog}
        onClose={() => setShowEditDialog(false)}
        team={team}
        onSubmit={(name, icon, taskPrefix) =>
          updateTeamMutation.mutate({ name, icon, task_prefix: taskPrefix ?? null })
        }
        isPending={updateTeamMutation.isPending}
      />

      {/* Delete Team Confirmation */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Agentic Team</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &ldquo;{team.name}&rdquo;? All
              nodes and connections will be removed. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteTeamMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
