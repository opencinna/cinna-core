import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useCallback } from "react"
import { Plus, Circle, CheckCircle2, List, Archive, Loader2, LayoutDashboard, Bot, Users } from "lucide-react"

import { TasksService, AgentsService } from "@/client"
import type { InputTaskPublicExtended } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import { CreateTaskDialog } from "@/components/Tasks/CreateTaskDialog"
import { TaskBoard } from "@/components/Tasks/TaskBoard"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import useWorkspace from "@/hooks/useWorkspace"
import { getColorPreset } from "@/utils/colorPresets"

export const Route = createFileRoute("/_layout/tasks/")({
  component: TasksList,
})

type ViewMode = "list" | "board"

type StatusFilter = "active" | "completed" | "archived" | "all"

// Status indicator dot colors matching TaskStatusBadge
const STATUS_COLORS: Record<string, string> = {
  new: "bg-gray-400",
  refining: "bg-purple-500",
  open: "bg-gray-400",
  in_progress: "bg-blue-500",
  blocked: "bg-amber-500",
  completed: "bg-green-500",
  error: "bg-red-500",
  cancelled: "bg-red-400",
  archived: "bg-gray-400",
  running: "bg-blue-500",
  pending_input: "bg-amber-500",
}

function getDateGroup(dateStr: string): string {
  const date = new Date(dateStr.endsWith("Z") ? dateStr : dateStr + "Z")
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const lastWeek = new Date(today)
  lastWeek.setDate(lastWeek.getDate() - 7)

  if (date >= today) return "Today"
  if (date >= yesterday) return "Yesterday"
  if (date >= lastWeek) return "Last week"
  return "Older"
}

const DATE_GROUP_ORDER = ["Today", "Yesterday", "Last week", "Older"]

function groupTasksByDate(tasks: InputTaskPublicExtended[]): { label: string; tasks: InputTaskPublicExtended[] }[] {
  const sorted = [...tasks].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
  )
  const groups: Record<string, InputTaskPublicExtended[]> = {}
  for (const task of sorted) {
    const group = getDateGroup(task.updated_at)
    ;(groups[group] ??= []).push(task)
  }
  return DATE_GROUP_ORDER
    .filter((label) => groups[label]?.length)
    .map((label) => ({ label, tasks: groups[label] }))
}

function TasksList() {
  const { setHeaderContent } = usePageHeader()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { activeWorkspaceId } = useWorkspace()
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active")
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [viewMode, setViewMode] = useState<ViewMode>("board")

  // Real-time invalidation for task events
  const handleTaskEvent = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["tasks"] })
  }, [queryClient])

  useMultiEventSubscription(
    [
      EventTypes.TASK_STATUS_CHANGED,
      EventTypes.TASK_SUBTASK_CREATED,
      EventTypes.SUBTASK_COMPLETED,
      EventTypes.TASK_COMMENT_ADDED,
    ],
    handleTaskEvent,
  )

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

  const { data: agentsData } = useQuery({
    queryKey: ["agents", activeWorkspaceId],
    queryFn: () =>
      AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: activeWorkspaceId ?? "",
      }),
  })

  const getAgentColorPreset = (agentId: string | null | undefined) => {
    if (!agentId || !agentsData?.data) return null
    const agent = agentsData.data.find((a) => a.id === agentId)
    return agent ? getColorPreset(agent.ui_color_preset) : null
  }

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center justify-between w-full gap-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Tasks</h1>
          <p className="text-xs text-muted-foreground">
            Manage and refine your incoming tasks
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center border rounded-md overflow-hidden">
            <button
              onClick={() => setViewMode("board")}
              className={cn(
                "px-2.5 py-1.5 text-xs flex items-center gap-1.5 transition-colors",
                viewMode === "board"
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-muted"
              )}
            >
              <LayoutDashboard className="h-3.5 w-3.5" />
              Board
            </button>
            <button
              onClick={() => setViewMode("list")}
              className={cn(
                "px-2.5 py-1.5 text-xs flex items-center gap-1.5 transition-colors",
                viewMode === "list"
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-muted"
              )}
            >
              <List className="h-3.5 w-3.5" />
              List
            </button>
          </div>
          <Button onClick={() => setCreateDialogOpen(true)} size="sm">
            <Plus className="h-4 w-4 mr-2" />
            New Task
          </Button>
        </div>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, viewMode])

  const handleTaskClick = (task: InputTaskPublicExtended) => {
    navigate({
      to: "/task/$taskId",
      params: { taskId: task.short_code || task.id },
    })
  }

  const handleCreated = (taskId: string) => {
    navigate({
      to: "/task/$taskId",
      params: { taskId },
    })
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading tasks</p>
      </div>
    )
  }

  const tasks = tasksData?.data || []

  if (viewMode === "board") {
    return (
      <>
        <div className="p-6 md:p-8 overflow-y-auto h-full" key={activeWorkspaceId ?? "default"}>
          <div className="mx-auto max-w-7xl">
            <TaskBoard />
          </div>
        </div>
        <CreateTaskDialog
          open={createDialogOpen}
          onOpenChange={setCreateDialogOpen}
          onCreated={handleCreated}
        />
      </>
    )
  }

  const dateGroups = groupTasksByDate(tasks)

  return (
    <div className="p-6 md:p-8 overflow-y-auto h-full" key={activeWorkspaceId ?? "default"}>
      <div className="mx-auto max-w-7xl">
        <div className="flex gap-6 items-start">
          {/* Status filter sidebar */}
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

          {/* Task table */}
          <div className="flex-1 relative min-w-0">
            {isFetching && (
              <div className="absolute inset-0 bg-background/50 backdrop-blur-[1px] z-10 flex items-center justify-center rounded-lg">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              </div>
            )}

            {tasks.length === 0 && !isLoading ? (
              <div className="text-center py-12 border-2 border-dashed rounded-lg">
                <p className="text-muted-foreground">
                  {statusFilter === "archived" ? "No archived tasks" : "No tasks yet"}
                </p>
              </div>
            ) : (
              <div className="space-y-6">
                {dateGroups.map((group) => (
                  <div key={group.label}>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2 px-1">
                      {group.label}
                    </h3>
                    <div className="border rounded-lg divide-y">
                      {group.tasks.map((task) => (
                        <div
                          key={task.id}
                          onClick={() => handleTaskClick(task)}
                          className="flex items-center gap-3 px-3 py-2.5 cursor-pointer transition-colors hover:bg-muted/50"
                        >
                          {/* Status dot */}
                          <span
                            className={cn(
                              "h-2 w-2 rounded-full shrink-0",
                              STATUS_COLORS[task.status] || "bg-gray-400"
                            )}
                            title={task.status}
                          />

                          {/* Short code */}
                          <span className="text-xs text-muted-foreground font-mono shrink-0 w-16 truncate">
                            {task.short_code || "—"}
                          </span>

                          {/* Title */}
                          <span className="text-sm truncate flex-1 min-w-0">
                            {task.title || task.current_description}
                          </span>

                          {/* Team / Agent badge */}
                          <span className="shrink-0 max-w-[160px]">
                            {task.team_name ? (
                              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-muted text-muted-foreground truncate">
                                <Users className="h-3 w-3 shrink-0" />
                                <span className="truncate">{task.team_name}</span>
                              </span>
                            ) : task.agent_name ? (
                              (() => {
                                const preset = getAgentColorPreset(task.selected_agent_id)
                                return (
                                  <span
                                    className={cn(
                                      "inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium truncate",
                                      preset ? `${preset.badgeBg} ${preset.badgeText}` : "bg-muted text-muted-foreground"
                                    )}
                                  >
                                    <Bot className="h-3 w-3 shrink-0" />
                                    <span className="truncate">{task.agent_name}</span>
                                  </span>
                                )
                              })()
                            ) : (
                              <span className="text-xs text-muted-foreground">—</span>
                            )}
                          </span>

                          {/* Last activity */}
                          <span className="shrink-0 w-24 text-right">
                            <RelativeTime
                              timestamp={task.updated_at}
                              className="text-xs text-muted-foreground"
                              showTooltip
                            />
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
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
    </div>
  )
}
