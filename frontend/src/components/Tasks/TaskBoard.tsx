import { useCallback } from "react"
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Bot, ClipboardList, ArchiveIcon } from "lucide-react"

import { TasksService } from "@/client"
import type { InputTaskPublicExtended } from "@/client"
import { Skeleton } from "@/components/ui/skeleton"
import { Badge } from "@/components/ui/badge"
import { TaskShortCodeBadge } from "@/components/Tasks/TaskShortCodeBadge"
import { TaskPriorityBadge } from "@/components/Tasks/TaskPriorityBadge"
import { SubtaskProgressChip } from "@/components/Tasks/SubtaskProgressChip"
import { cn } from "@/lib/utils"
import useWorkspace from "@/hooks/useWorkspace"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"

interface TaskBoardProps {
  teamId?: string
}

const OPEN_STATUSES = ["new", "refining", "open"]
const BOARD_COLUMNS = [
  { key: "open", label: "Open", statuses: OPEN_STATUSES },
  { key: "in_progress", label: "In Progress", statuses: ["in_progress"] },
  { key: "blocked", label: "Blocked", statuses: ["blocked"] },
  { key: "completed", label: "Completed", statuses: ["completed"] },
] as const

function TaskCard({ task }: { task: InputTaskPublicExtended }) {
  const navigate = useNavigate()

  const handleClick = () => {
    navigate({ to: "/task/$taskId", params: { taskId: task.short_code || task.id } })
  }

  const displayTitle = task.title || task.current_description

  return (
    <div
      onClick={handleClick}
      className="group p-3 rounded-lg border bg-card cursor-pointer transition-all hover:shadow-sm hover:border-border/80 space-y-2"
    >
      {/* Short code + priority row */}
      <div className="flex items-center justify-between gap-2">
        {task.short_code ? (
          <TaskShortCodeBadge
            shortCode={task.short_code}
            status={task.status}
            onClick={handleClick}
          />
        ) : (
          <span className="text-xs text-muted-foreground font-mono">—</span>
        )}
        <TaskPriorityBadge priority={task.priority || "normal"} />
      </div>

      {/* Title */}
      <p className="text-sm font-medium line-clamp-2 leading-snug">
        {displayTitle}
      </p>

      {/* Footer row */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {task.agent_name ? (
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground truncate">
              <Bot className="h-3 w-3 shrink-0" />
              <span className="truncate">{task.agent_name}</span>
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">Unassigned</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {(task.subtask_count ?? 0) > 0 && (
            <SubtaskProgressChip
              total={task.subtask_count ?? 0}
              completed={task.subtask_completed_count ?? 0}
              taskId={task.id}
            />
          )}
          {task.team_name && (
            <span className="text-xs text-muted-foreground bg-muted px-1.5 py-0.5 rounded truncate max-w-[80px]">
              {task.team_name}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

function TaskCardSkeleton() {
  return (
    <div className="p-3 rounded-lg border bg-card space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-4 w-12" />
      </div>
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-3 w-3/4" />
      <div className="flex items-center justify-between gap-2">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-10" />
      </div>
    </div>
  )
}

function BoardColumnSkeleton({ label }: { label: string }) {
  return (
    <div className="flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between px-1">
        <span className="text-sm font-medium text-muted-foreground">{label}</span>
        <Skeleton className="h-5 w-5 rounded-full" />
      </div>
      <div className="flex flex-col gap-2">
        <TaskCardSkeleton />
        <TaskCardSkeleton />
        <TaskCardSkeleton />
      </div>
    </div>
  )
}

function BoardColumn({
  label,
  columnKey,
  tasks,
  onArchiveAll,
  isArchiving,
}: {
  label: string
  columnKey: string
  tasks: InputTaskPublicExtended[]
  onArchiveAll?: () => void
  isArchiving?: boolean
}) {
  return (
    <div className="flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between px-1">
        <span className="text-sm font-medium text-muted-foreground">{label}</span>
        <div className="flex items-center gap-1.5">
          {columnKey === "completed" && tasks.length > 0 && (
            <button
              onClick={onArchiveAll}
              disabled={isArchiving}
              title="Archive all completed tasks"
              className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
            >
              <ArchiveIcon className="h-3.5 w-3.5" />
            </button>
          )}
          <Badge
            variant="secondary"
            className="h-5 min-w-5 px-1.5 text-[10px] tabular-nums"
          >
            {tasks.length}
          </Badge>
        </div>
      </div>
      <div className="flex flex-col gap-2 min-h-[120px]">
        {tasks.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
        {tasks.length === 0 && (
          <div className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">
            No tasks
          </div>
        )}
      </div>
    </div>
  )
}

export function TaskBoard({ teamId }: TaskBoardProps) {
  const { workspaceFilter } = useWorkspace()
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ["tasks", "board", workspaceFilter, teamId],
    queryFn: ({ queryKey }) => {
      const [, , workspaceId, tid] = queryKey
      return TasksService.listTasks({
        rootOnly: true,
        userWorkspaceId: workspaceId as string | undefined,
        teamId: (tid as string) || undefined,
        limit: 200,
      })
    },
  })

  // Subscribe to task real-time events and invalidate board query
  const handleTaskBoardEvent = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["tasks", "board"] })
  }, [queryClient])

  useMultiEventSubscription(
    [
      EventTypes.TASK_STATUS_CHANGED,
      EventTypes.TASK_SUBTASK_CREATED,
      EventTypes.SUBTASK_COMPLETED,
    ],
    handleTaskBoardEvent,
  )

  const allTasks = data?.data ?? []

  const tasksByColumn = Object.fromEntries(
    BOARD_COLUMNS.map((col) => [
      col.key,
      allTasks.filter((t) => (col.statuses as readonly string[]).includes(t.status)),
    ])
  )

  const archiveAllMutation = useMutation({
    mutationFn: async () => {
      const completedTasks = tasksByColumn["completed"] ?? []
      await Promise.all(
        completedTasks.map((t) => TasksService.archiveTask({ id: t.id }))
      )
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
    },
  })

  const handleArchiveAll = useCallback(() => {
    archiveAllMutation.mutate()
  }, [archiveAllMutation])

  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Loading skeleton */}
      {isLoading ? (
        <div className={cn("grid gap-4 flex-1", "grid-cols-1 sm:grid-cols-2 lg:grid-cols-4")}>
          {BOARD_COLUMNS.map((col) => (
            <BoardColumnSkeleton key={col.key} label={col.label} />
          ))}
        </div>
      ) : allTasks.length === 0 ? (
        /* Global empty state — no tasks at all */
        <div className="flex flex-col items-center justify-center py-16 gap-4 text-center flex-1">
          <div className="rounded-full bg-muted p-4">
            <ClipboardList className="h-8 w-8 text-muted-foreground" />
          </div>
          <div className="space-y-1">
            <p className="text-base font-medium">No tasks yet</p>
            <p className="text-sm text-muted-foreground">
              Create your first task to get started
            </p>
          </div>
        </div>
      ) : (
        <div className={cn("grid gap-4 flex-1", "grid-cols-1 sm:grid-cols-2 lg:grid-cols-4")}>
          {BOARD_COLUMNS.map((col) => (
            <BoardColumn
              key={col.key}
              columnKey={col.key}
              label={col.label}
              tasks={tasksByColumn[col.key] ?? []}
              onArchiveAll={col.key === "completed" ? handleArchiveAll : undefined}
              isArchiving={col.key === "completed" ? archiveAllMutation.isPending : undefined}
            />
          ))}
        </div>
      )}

    </div>
  )
}
