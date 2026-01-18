import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, KeyboardEvent, useRef } from "react"
import {
  Play,
  ArrowLeft,
  Edit,
  Loader2,
  Send,
  Check,
  EllipsisVertical,
  Trash2,
  Bot,
} from "lucide-react"

import { TasksService, AgentsService } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import type { RefinementHistoryItem } from "@/components/Tasks/RefinementChat"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
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
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useWorkspace from "@/hooks/useWorkspace"
import useCustomToast from "@/hooks/useCustomToast"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { getColorPreset } from "@/utils/colorPresets"
import { cn } from "@/lib/utils"

export const Route = createFileRoute("/_layout/task/$taskId")({
  component: TaskDetail,
})

function TaskDetail() {
  const { taskId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [isEditing, setIsEditing] = useState(false)
  const [editedDescription, setEditedDescription] = useState("")
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)
  const [refinementComment, setRefinementComment] = useState("")
  const [chatHistoryOpen, setChatHistoryOpen] = useState(false)
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false)
  const refinementInputRef = useRef<HTMLTextAreaElement>(null)

  const {
    data: task,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => TasksService.getTask({ id: taskId }),
  })

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

  // Fetch sessions spawned by this task
  const { data: sessionsData } = useQuery({
    queryKey: ["task-sessions", taskId],
    queryFn: () => TasksService.listTaskSessions({ id: taskId }),
  })

  const updateMutation = useMutation({
    mutationFn: (data: { current_description?: string; selected_agent_id?: string }) =>
      TasksService.updateTask({ id: taskId, requestBody: data }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["task", taskId] })
      // Only show toast and exit editing mode for description changes
      if (variables.current_description !== undefined) {
        setIsEditing(false)
        showSuccessToast("Task updated")
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to update task")
    },
  })

  const executeMutation = useMutation({
    mutationFn: () =>
      TasksService.executeTask({ id: taskId, requestBody: { mode: "conversation" } }),
    onSuccess: (result) => {
      if (result.success && result.session_id) {
        queryClient.invalidateQueries({ queryKey: ["task", taskId] })
        queryClient.invalidateQueries({ queryKey: ["task-sessions", taskId] })
        navigate({
          to: "/session/$sessionId",
          params: { sessionId: result.session_id },
          search: { initialMessage: task?.current_description, fileIds: undefined },
        })
      } else {
        showErrorToast(result.error || "Failed to execute task")
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to execute task")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => TasksService.deleteTask({ id: taskId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
      navigate({ to: "/tasks" })
      showSuccessToast("Task deleted")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to delete task")
    },
  })

  const refineMutation = useMutation({
    mutationFn: (userComment: string) =>
      TasksService.refineTask({
        id: taskId,
        requestBody: { user_comment: userComment },
      }),
    onSuccess: (result) => {
      if (result.success && result.refined_description) {
        queryClient.invalidateQueries({ queryKey: ["task", taskId] })
        setEditedDescription(result.refined_description)
      }
      setRefinementComment("")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to refine task")
    },
  })

  // Get selected agent info for header
  const headerSelectedAgent = agentsData?.data?.find(a => a.id === task?.selected_agent_id)
  const headerAgentColorPreset = headerSelectedAgent ? getColorPreset(headerSelectedAgent.ui_color_preset) : null

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center gap-4 w-full">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => navigate({ to: "/tasks" })}
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="flex-1 min-w-0">
          <h1 className="text-lg font-semibold truncate">Task Refinement</h1>
          <p className="text-xs text-muted-foreground">
            Refine and prepare your task for execution
          </p>
        </div>
        {task && (
          <div className="flex items-center gap-2">
            {/* Edit Task button */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsEditing(!isEditing)}
              className="h-8"
            >
              <Edit className="h-4 w-4 mr-1.5" />
              Edit Task
            </Button>
            {/* Agent selector in header with robot icon */}
            <button
              onClick={() => setAgentSelectorOpen(true)}
              disabled={updateMutation.isPending}
              className={cn(
                "h-8 px-3 rounded-md text-sm font-medium transition-all flex items-center gap-1.5",
                headerAgentColorPreset
                  ? `${headerAgentColorPreset.badgeBg} ${headerAgentColorPreset.badgeText} ${headerAgentColorPreset.badgeHover}`
                  : "bg-muted text-muted-foreground hover:bg-muted/80"
              )}
            >
              <Bot className="h-4 w-4" />
              {headerSelectedAgent ? headerSelectedAgent.name : "Select Agent"}
            </button>
            {/* Dropdown menu */}
            <DropdownMenu open={headerMenuOpen} onOpenChange={setHeaderMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon">
                  <EllipsisVertical className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() => {
                    setHeaderMenuOpen(false)
                    setDeleteDialogOpen(true)
                  }}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  Delete Task
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )}
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, task, navigate, headerSelectedAgent, headerAgentColorPreset, headerMenuOpen, updateMutation.isPending, isEditing])

  useEffect(() => {
    if (task) {
      setEditedDescription(task.current_description)
    }
  }, [task])

  const handleSaveDescription = () => {
    if (editedDescription !== task?.current_description) {
      updateMutation.mutate({ current_description: editedDescription })
    } else {
      setIsEditing(false)
    }
  }

  const handleAgentSelect = (agentId: string) => {
    updateMutation.mutate({ selected_agent_id: agentId || undefined })
    setAgentSelectorOpen(false)
    // Focus the refinement input after modal closes
    setTimeout(() => {
      refinementInputRef.current?.focus()
    }, 100)
  }

  const handleRefinementSubmit = () => {
    if (!refinementComment.trim() || refineMutation.isPending) return
    refineMutation.mutate(refinementComment.trim())
  }

  const handleRefinementKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleRefinementSubmit()
    }
  }

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !task) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading task</p>
        <Button
          variant="outline"
          onClick={() => navigate({ to: "/tasks" })}
          className="mt-4"
        >
          Back to Tasks
        </Button>
      </div>
    )
  }

  const agents = agentsData?.data || []
  const sessions = sessionsData?.data || []
  const canExecute =
    task.selected_agent_id &&
    !["running", "pending_input", "completed", "archived"].includes(task.status)

  // Get the latest AI reply from refinement history
  const refinementHistory = (task.refinement_history || []) as RefinementHistoryItem[]
  const latestAiReply = [...refinementHistory].reverse().find(item => item.role === "ai")

  return (
    <div className="flex flex-col h-full">
      {/* Main content - centered task text */}
      <div className="flex-1 overflow-auto flex items-start justify-center p-6">
        <div className="w-full max-w-3xl">
          {isEditing ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Editing task description</span>
                <div className="flex items-center gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditedDescription(task.current_description)
                      setIsEditing(false)
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    size="sm"
                    onClick={handleSaveDescription}
                    disabled={updateMutation.isPending}
                  >
                    {updateMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      "Save"
                    )}
                  </Button>
                </div>
              </div>
              <Textarea
                value={editedDescription}
                onChange={(e) => setEditedDescription(e.target.value)}
                className="min-h-[300px] resize-none text-base"
                placeholder="Task description..."
              />
            </div>
          ) : (
            <div className="prose prose-lg dark:prose-invert max-w-none">
              <MarkdownRenderer
                content={task.current_description}
                className="text-base leading-relaxed"
              />
            </div>
          )}
        </div>
      </div>

      {/* Footer with 3 sections - full height elements */}
      <div className="border-t bg-background/60 p-4 shrink-0 relative">
        {/* Chat history overlay - game console style, 1/3 width aligned left */}
        {chatHistoryOpen && refinementHistory.length > 0 && (
          <div
            className="absolute bottom-full left-4 w-1/3 bg-slate-900/90 backdrop-blur-sm border border-slate-700 rounded-t-lg max-h-[50vh] overflow-y-auto"
            onClick={() => setChatHistoryOpen(false)}
          >
            <div className="p-3 space-y-2">
              {refinementHistory.map((item, index) => (
                <div
                  key={index}
                  className={cn(
                    "flex gap-2",
                    item.role === "user" ? "flex-row-reverse" : ""
                  )}
                  onClick={(e) => e.stopPropagation()}
                >
                  <div
                    className={cn(
                      "max-w-[85%] rounded-lg px-2.5 py-1.5",
                      item.role === "user"
                        ? "bg-primary text-primary-foreground"
                        : "bg-slate-800 text-slate-200"
                    )}
                  >
                    <p className="text-xs whitespace-pre-wrap">{item.content}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-stretch gap-2 max-w-7xl mx-auto h-[80px]">
          {/* Left section - Latest AI reply (fills space to left, 30% transparent, compact) */}
          {latestAiReply && (
            <div
              className={cn(
                "flex-1 min-w-0 rounded-md px-2.5 py-2 cursor-pointer transition-all overflow-hidden relative",
                "bg-slate-500/30 dark:bg-slate-600/30",
                "border border-slate-400/30 dark:border-slate-500/30",
                "hover:bg-slate-500/40 dark:hover:bg-slate-600/40"
              )}
              onClick={() => setChatHistoryOpen(!chatHistoryOpen)}
            >
              <Bot className="h-3 w-3 text-slate-400/60 dark:text-slate-500/60 absolute top-1.5 left-1.5" />
              <p className="text-xs text-slate-600 dark:text-slate-300 line-clamp-4 leading-relaxed pl-4">
                {latestAiReply.content}
              </p>
            </div>
          )}

          {/* Center section - Message input (full height) */}
          <div className={cn("min-w-0", latestAiReply ? "flex-[2]" : "flex-1")}>
            <Textarea
              ref={refinementInputRef}
              value={refinementComment}
              onChange={(e) => setRefinementComment(e.target.value)}
              onKeyDown={handleRefinementKeyDown}
              placeholder="Add details, ask for changes, or request clarification..."
              className="h-full resize-none text-sm"
              disabled={refineMutation.isPending}
            />
          </div>

          {/* Right section - Big square buttons */}
          <div className="flex items-stretch gap-2 shrink-0">
            {/* Send message button - big square */}
            <Button
              variant="outline"
              onClick={handleRefinementSubmit}
              disabled={!refinementComment.trim() || refineMutation.isPending}
              className="h-full w-[80px] flex-col gap-1"
            >
              {refineMutation.isPending ? (
                <Loader2 className="h-5 w-5 animate-spin" />
              ) : (
                <>
                  <Send className="h-5 w-5" />
                  <span className="text-xs">Send</span>
                </>
              )}
            </Button>

            {/* Execute button - big square prominent */}
            <Button
              onClick={() => executeMutation.mutate()}
              disabled={!canExecute || executeMutation.isPending}
              className="h-full w-[80px] flex-col gap-1"
            >
              {executeMutation.isPending ? (
                <Loader2 className="h-5 w-5 animate-spin" />
              ) : (
                <>
                  <Play className="h-5 w-5" />
                  <span className="text-xs">{sessions.length > 0 ? "Run Again" : "Execute"}</span>
                </>
              )}
            </Button>
          </div>
        </div>
      </div>

      {/* Agent selection modal */}
      <Dialog open={agentSelectorOpen} onOpenChange={setAgentSelectorOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Select Agent</DialogTitle>
          </DialogHeader>
          <div className="flex flex-wrap gap-2 pt-4">
            {agents.map((agent) => {
              const colorPreset = getColorPreset(agent.ui_color_preset)
              const isSelected = task.selected_agent_id === agent.id
              return (
                <button
                  key={agent.id}
                  className={cn(
                    "cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2",
                    colorPreset.badgeBg,
                    colorPreset.badgeText,
                    colorPreset.badgeHover,
                    isSelected && colorPreset.badgeOutline
                  )}
                  onClick={() => handleAgentSelect(agent.id)}
                >
                  {agent.name}
                  {isSelected && <Check className="h-4 w-4" />}
                </button>
              )
            })}
            {agents.length === 0 && (
              <p className="text-sm text-muted-foreground">No agents available</p>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Task</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this task? This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Delete"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
