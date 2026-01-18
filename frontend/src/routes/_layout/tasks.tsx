import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useCallback } from "react"
import { Plus, MessageSquare, Play, Sparkles, Circle, CheckCircle2, List, Archive, Loader2, MoreVertical, Trash2, Layers } from "lucide-react"

import { TasksService, AgentsService } from "@/client"
import type { InputTaskPublicExtended } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import { TaskStatusBadge } from "@/components/Tasks/TaskStatusBadge"
import { CreateTaskDialog } from "@/components/Tasks/CreateTaskDialog"
import { TaskSessionsModal } from "@/components/Tasks/TaskSessionsModal"
import { TaskTodoProgress, type TodoItem } from "@/components/Tasks/TaskTodoProgress"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"
import useWorkspace from "@/hooks/useWorkspace"
import { getColorPreset } from "@/utils/colorPresets"

export const Route = createFileRoute("/_layout/tasks")({
  component: TasksList,
})

type StatusFilter = "active" | "completed" | "archived" | "all"

function TasksList() {
  const { setHeaderContent } = usePageHeader()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { activeWorkspaceId } = useWorkspace()
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active")
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [sessionsModalTaskId, setSessionsModalTaskId] = useState<string | null>(null)

  // Real-time to-do progress tracking for tasks
  const [taskTodos, setTaskTodos] = useState<Record<string, TodoItem[]>>({})

  // Subscribe to TASK_TODO_UPDATED events for real-time updates
  const handleTodoUpdate = useCallback((event: { meta?: { task_id?: string; todos?: TodoItem[] } }) => {
    const { task_id, todos } = event.meta || {}
    if (task_id && todos) {
      setTaskTodos((prev) => ({ ...prev, [task_id]: todos }))
    }
  }, [])
  useEventSubscription(EventTypes.TASK_TODO_UPDATED, handleTodoUpdate)

  const autoRefineMutation = useMutation({
    mutationFn: (taskId: string) =>
      TasksService.refineTask({
        id: taskId,
        requestBody: { user_comment: "Please analyze and refine this task automatically." },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  const executeMutation = useMutation({
    mutationFn: (params: { taskId: string; description: string }) =>
      TasksService.executeTask({
        id: params.taskId,
        requestBody: {},
      }),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
      if (data.session_id) {
        navigate({
          to: "/session/$sessionId",
          params: { sessionId: data.session_id },
          search: { initialMessage: variables.description, fileIds: undefined },
        })
      }
    },
  })

  const handleAutoRefine = (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    autoRefineMutation.mutate(taskId)
  }

  const handleExecute = (e: React.MouseEvent, taskId: string, description: string) => {
    e.stopPropagation()
    executeMutation.mutate({ taskId, description })
  }

  const archiveMutation = useMutation({
    mutationFn: (taskId: string) => TasksService.archiveTask({ id: taskId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (taskId: string) => TasksService.deleteTask({ id: taskId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  const handleArchive = (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    archiveMutation.mutate(taskId)
  }

  const handleDelete = (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    if (confirm("Are you sure you want to delete this task?")) {
      deleteMutation.mutate(taskId)
    }
  }

  const {
    data: tasksData,
    isLoading,
    isFetching,
    error,
  } = useQuery({
    queryKey: ["tasks", statusFilter, activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, status, workspaceId] = queryKey
      return TasksService.listTasks({
        status: status as string,
        userWorkspaceId: workspaceId ?? "",
      })
    },
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

  // Fetch counts for filters
  const { data: activeCount } = useQuery({
    queryKey: ["tasks", "active", activeWorkspaceId, "count"],
    queryFn: () =>
      TasksService.listTasks({
        status: "active",
        userWorkspaceId: activeWorkspaceId ?? "",
      }),
    select: (data) => data.count,
  })

  const { data: completedCount } = useQuery({
    queryKey: ["tasks", "completed", activeWorkspaceId, "count"],
    queryFn: () =>
      TasksService.listTasks({
        status: "completed",
        userWorkspaceId: activeWorkspaceId ?? "",
      }),
    select: (data) => data.count,
  })

  // Initialize taskTodos from API response (persisted todos)
  useEffect(() => {
    if (tasksData?.data) {
      const initialTodos: Record<string, TodoItem[]> = {}
      for (const task of tasksData.data) {
        if (task.todo_progress && Array.isArray(task.todo_progress) && task.todo_progress.length > 0) {
          initialTodos[task.id] = task.todo_progress as TodoItem[]
        }
      }
      if (Object.keys(initialTodos).length > 0) {
        setTaskTodos((prev) => ({ ...initialTodos, ...prev }))
      }
    }
  }, [tasksData?.data])

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center justify-between w-full gap-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Tasks</h1>
          <p className="text-xs text-muted-foreground">
            Manage and refine your incoming tasks
          </p>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)} size="sm">
          <Plus className="h-4 w-4 mr-2" />
          New Task
        </Button>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  const handleTaskClick = (task: InputTaskPublicExtended) => {
    navigate({
      to: "/task/$taskId",
      params: { taskId: task.id },
    })
  }

  const handleGoToSession = (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation()
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
      search: { initialMessage: undefined, fileIds: undefined },
    })
  }

  const handleOpenSessionsModal = (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    setSessionsModalTaskId(taskId)
  }

  const handleCreated = (taskId: string) => {
    navigate({
      to: "/task/$taskId",
      params: { taskId },
    })
  }

  // Get agent color preset by ID
  const getAgentColorPreset = (agentId: string | null | undefined) => {
    if (!agentId || !agentsData?.data) return null
    const agent = agentsData.data.find((a) => a.id === agentId)
    return agent ? getColorPreset(agent.ui_color_preset) : null
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading tasks</p>
      </div>
    )
  }

  const tasks = tasksData?.data || []

  return (
    <div className="p-6 md:p-8 overflow-y-auto h-full" key={activeWorkspaceId ?? "default"}>
      <div className="mx-auto max-w-7xl">
        <div className="flex gap-6 items-start">
          {/* Filters sidebar */}
          <div className="w-48 flex-shrink-0">
            <div className="space-y-1">
                {([
                  { key: "active" as StatusFilter, label: "Active", icon: Circle, count: activeCount },
                  { key: "completed" as StatusFilter, label: "Completed", icon: CheckCircle2, count: completedCount },
                  { key: "all" as StatusFilter, label: "All", icon: List, count: (activeCount ?? 0) + (completedCount ?? 0) },
                ]).map((filter) => (
                  <button
                    key={filter.key}
                    onClick={() => setStatusFilter(filter.key)}
                    className={cn(
                      "w-full flex items-center justify-between px-3 py-2 text-sm rounded-md transition-all",
                      statusFilter === filter.key
                        ? "bg-primary/10 text-primary font-medium"
                        : "hover:bg-muted"
                    )}
                  >
                    <span className="flex items-center gap-2">
                      <filter.icon className="h-4 w-4" />
                      {filter.label}
                    </span>
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {filter.count ?? 0}
                    </span>
                  </button>
                ))}

                <div className="border-t my-2" />

                <button
                  onClick={() => setStatusFilter("archived")}
                  className={cn(
                    "w-full flex items-center gap-2 px-3 py-2 text-sm rounded-md transition-all",
                    statusFilter === "archived"
                      ? "bg-primary/10 text-primary font-medium"
                      : "hover:bg-muted"
                  )}
                >
                  <Archive className="h-4 w-4" />
                  Archived
                </button>
            </div>
          </div>

          {/* Tasks list */}
          <div className="flex-1 relative">
            {/* Loading overlay */}
            {isFetching && (
              <div className="absolute inset-0 bg-background/50 backdrop-blur-[1px] z-10 flex items-center justify-center rounded-lg">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              </div>
            )}

            {tasks.length === 0 && !isLoading ? (
              <div className="text-center py-12 border-2 border-dashed rounded-lg">
                <p className="text-muted-foreground mb-4">
                  {statusFilter === "archived" ? "No archived tasks" : "No tasks yet"}
                </p>
                {statusFilter !== "archived" && (
                  <Button onClick={() => setCreateDialogOpen(true)}>
                    <Plus className="h-4 w-4 mr-2" />
                    Create your first task
                  </Button>
                )}
              </div>
            ) : (
              <div className="space-y-3 min-h-[100px]">
                {tasks.map((task) => {
                  const colorPreset = getAgentColorPreset(task.selected_agent_id)

                  return (
                    <div
                      key={task.id}
                      onClick={() => handleTaskClick(task)}
                      className={cn(
                        "p-4 rounded-lg border bg-card/70 cursor-pointer transition-all hover:bg-muted/50"
                      )}
                    >
                      <div className="flex justify-between gap-4">
                        {/* Left: content */}
                        <div className="flex-1 min-w-0 space-y-2">
                          <div className="flex items-center gap-2 flex-wrap">
                            <TaskStatusBadge status={task.status} />
                            {task.agent_name && colorPreset && (
                              <span
                                className={cn(
                                  "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium",
                                  colorPreset.badgeBg,
                                  colorPreset.badgeText
                                )}
                              >
                                {task.agent_name}
                              </span>
                            )}
                          </div>
                          <p className="text-sm line-clamp-2">
                            {task.current_description}
                          </p>
                          <div className="flex items-center gap-4 text-xs text-muted-foreground">
                            <RelativeTime timestamp={task.created_at} />
                            {task.refinement_history &&
                              (task.refinement_history as Array<{ role: string }>).filter((m) => m.role === "user").length > 0 && (
                                <span className="flex items-center gap-1">
                                  <MessageSquare className="h-3 w-3" />
                                  {(task.refinement_history as Array<{ role: string }>).filter((m) => m.role === "user").length} refinements
                                </span>
                              )}
                            {/* To-do progress (real-time updates) */}
                            {taskTodos[task.id] && taskTodos[task.id].length > 0 && (
                              <TaskTodoProgress todos={taskTodos[task.id]} className="mt-0" />
                            )}
                          </div>
                        </div>

                        {/* Right: dropdown at top, buttons at bottom */}
                        <div className="flex flex-col items-end justify-between flex-shrink-0">
                          {/* Dropdown menu */}
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                              <Button variant="ghost" size="icon" className="h-8 w-8">
                                <MoreVertical className="h-4 w-4" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem onClick={(e) => handleArchive(e as unknown as React.MouseEvent, task.id)}>
                                <Archive className="h-4 w-4 mr-2" />
                                Archive
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                onClick={(e) => handleDelete(e as unknown as React.MouseEvent, task.id)}
                                className="text-destructive focus:text-destructive"
                              >
                                <Trash2 className="h-4 w-4 mr-2" />
                                Delete
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>

                          {/* Action buttons */}
                          <div className="flex items-center gap-2">
                            {task.status !== "completed" && (
                              <Button
                                size="icon"
                                variant="ghost"
                                onClick={(e) => handleAutoRefine(e, task.id)}
                                disabled={autoRefineMutation.isPending}
                                className="h-8 w-8 hover:text-amber-500 hover:bg-amber-500/10 transition-colors [&:hover_svg]:drop-shadow-[0_0_6px_rgba(251,191,36,0.6)]"
                                title="Auto-Refine"
                              >
                                <Sparkles className={cn(
                                  "h-4 w-4 transition-all",
                                  autoRefineMutation.isPending && "animate-pulse text-amber-500"
                                )} />
                              </Button>
                            )}
                            {(task.sessions_count ?? 0) > 1 && (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={(e) => handleOpenSessionsModal(e, task.id)}
                                className="text-muted-foreground"
                              >
                                <Layers className="h-3.5 w-3.5 mr-1" />
                                {task.sessions_count} sessions
                              </Button>
                            )}
                            {task.latest_session_id && (
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={(e) => handleGoToSession(e, task.latest_session_id!)}
                              >
                                Go to Session
                              </Button>
                            )}
                            <Button
                              size="sm"
                              onClick={(e) => handleExecute(e, task.id, task.current_description)}
                              disabled={executeMutation.isPending || !task.selected_agent_id}
                              title={!task.selected_agent_id ? "Select an agent first" : "Execute task"}
                            >
                              <Play className="h-4 w-4 mr-1" />
                              Execute
                            </Button>
                          </div>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      <CreateTaskDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        onCreated={handleCreated}
      />

      {sessionsModalTaskId && (
        <TaskSessionsModal
          taskId={sessionsModalTaskId}
          open={!!sessionsModalTaskId}
          onOpenChange={(open) => {
            if (!open) setSessionsModalTaskId(null)
          }}
        />
      )}
    </div>
  )
}
