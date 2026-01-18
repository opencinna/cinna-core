import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import {
  Play,
  ArrowLeft,
  Edit,
  Loader2,
  Trash2,
  FileText,
  ExternalLink,
} from "lucide-react"

import { TasksService, AgentsService } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import { TaskStatusBadge } from "@/components/Tasks/TaskStatusBadge"
import { RefinementChat, type RefinementHistoryItem } from "@/components/Tasks/RefinementChat"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
import useWorkspace from "@/hooks/useWorkspace"
import useCustomToast from "@/hooks/useCustomToast"
import { RelativeTime } from "@/components/Common/RelativeTime"
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task", taskId] })
      setIsEditing(false)
      showSuccessToast("Task updated")
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
            <TaskStatusBadge status={task.status} />
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setDeleteDialogOpen(true)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, task, navigate])

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

  const handleAgentChange = (agentId: string) => {
    updateMutation.mutate({ selected_agent_id: agentId || undefined })
  }

  const handleRefined = (newDescription: string) => {
    setEditedDescription(newDescription)
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

  const handleGoToSession = (sessionId: string) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
      search: { initialMessage: undefined, fileIds: undefined },
    })
  }

  return (
    <div className="h-full overflow-hidden flex">
      {/* Left panel - Task description */}
      <div className="w-1/2 border-r flex flex-col">
        <div className="p-4 border-b">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-medium">Task Description</h2>
            <div className="flex items-center gap-2">
              {isEditing ? (
                <>
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
                </>
              ) : (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsEditing(true)}
                >
                  <Edit className="h-4 w-4 mr-1" />
                  Edit
                </Button>
              )}
            </div>
          </div>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Agent</Label>
              <Select
                value={task.selected_agent_id || ""}
                onValueChange={handleAgentChange}
                disabled={updateMutation.isPending}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select an agent" />
                </SelectTrigger>
                <SelectContent>
                  {agents.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <Button
              className="w-full"
              onClick={() => executeMutation.mutate()}
              disabled={!canExecute || executeMutation.isPending}
            >
              {executeMutation.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Executing...
                </>
              ) : (
                <>
                  <Play className="h-4 w-4 mr-2" />
                  {sessions.length > 0 ? "Run Again" : "Execute Task"}
                </>
              )}
            </Button>

            {/* Sessions list */}
            {sessions.length > 0 && (
              <div className="mt-4 pt-4 border-t">
                <Label className="text-xs text-muted-foreground">
                  Sessions ({sessions.length})
                </Label>
                <div className="mt-2 space-y-2 max-h-40 overflow-y-auto">
                  {sessions.map((session) => (
                    <button
                      key={session.id}
                      onClick={() => handleGoToSession(session.id)}
                      className={cn(
                        "w-full flex items-center justify-between p-2 rounded-md text-left text-sm",
                        "hover:bg-muted transition-colors",
                        session.status === "active" && "bg-emerald-50 dark:bg-emerald-900/20"
                      )}
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span
                            className={cn(
                              "w-2 h-2 rounded-full",
                              session.status === "active"
                                ? "bg-emerald-500"
                                : session.status === "completed"
                                  ? "bg-slate-400"
                                  : session.status === "error"
                                    ? "bg-red-500"
                                    : "bg-yellow-500"
                            )}
                          />
                          <span className="truncate">
                            {session.title || "Untitled Session"}
                          </span>
                        </div>
                        <RelativeTime
                          timestamp={session.created_at}
                          className="text-xs text-muted-foreground mt-0.5"
                        />
                      </div>
                      <ExternalLink className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {isEditing ? (
            <Textarea
              value={editedDescription}
              onChange={(e) => setEditedDescription(e.target.value)}
              className="h-full resize-none"
              placeholder="Task description..."
            />
          ) : (
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <p className="whitespace-pre-wrap">{task.current_description}</p>
            </div>
          )}
        </div>

        {/* Original message (read-only) */}
        {task.original_message !== task.current_description && (
          <div className="p-4 border-t bg-muted/30">
            <div className="flex items-center gap-2 mb-2">
              <FileText className="h-4 w-4 text-muted-foreground" />
              <span className="text-xs text-muted-foreground font-medium">
                Original Request
              </span>
            </div>
            <p className="text-xs text-muted-foreground line-clamp-3">
              {task.original_message}
            </p>
          </div>
        )}
      </div>

      {/* Right panel - Refinement chat */}
      <div className="w-1/2 flex flex-col">
        <div className="p-4 border-b">
          <h2 className="font-medium">AI Refinement</h2>
          <p className="text-xs text-muted-foreground mt-1">
            Chat with AI to clarify and improve your task description
          </p>
        </div>
        <div className="flex-1 overflow-hidden">
          <RefinementChat
            taskId={taskId}
            history={(task.refinement_history || []) as RefinementHistoryItem[]}
            onRefined={handleRefined}
          />
        </div>
      </div>

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
