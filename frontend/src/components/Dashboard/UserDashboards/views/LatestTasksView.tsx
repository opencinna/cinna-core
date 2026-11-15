import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ClipboardList, Clock } from "lucide-react"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"

import { TasksService } from "@/client"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

interface LatestTasksViewProps {
  agentId: string
}

const STATUS_STYLES: Record<string, string> = {
  new: "text-violet-600",
  running: "text-blue-600",
  pending: "text-amber-600",
  error: "text-red-600",
  completed: "text-green-600",
  cancelled: "text-gray-500",
}

export function LatestTasksView({ agentId: _agentId }: LatestTasksViewProps) {
  // Note: The tasks API doesn't currently support filtering by agent_id.
  // We fetch recent tasks and show them. A future backend enhancement
  // could add agent_id filtering to the tasks list endpoint.
  const { data, isLoading } = useQuery({
    queryKey: ["dashboardBlockTasks", _agentId],
    queryFn: () =>
      TasksService.listTasks({
        limit: 5,
      }),
    refetchInterval: 30000,
  })

  if (isLoading) {
    return (
      <div className="p-3 space-y-2">
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-6 w-full" />
        ))}
      </div>
    )
  }

  const tasks = data?.data ?? []

  if (tasks.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-4 text-center">
        <ClipboardList className="h-8 w-8 text-muted-foreground mb-2" />
        <p className="text-sm text-muted-foreground">No recent tasks</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex-1 overflow-y-auto">
        {tasks.map((task) => {
          const timeAgo = task.updated_at
            ? formatRelativeTimestamp(task.updated_at)
            : null
          const statusStyle = STATUS_STYLES[task.status] ?? "text-gray-500"

          return (
            <div
              key={task.id}
              className="flex items-center gap-2 px-3 py-2 border-b last:border-0 hover:bg-muted/50 transition-colors"
            >
              <span
                className={cn("h-2 w-2 rounded-full shrink-0 bg-current", statusStyle)}
              />
              <span className="flex-1 text-xs truncate">{task.current_description || task.original_message}</span>
              {timeAgo && (
                <span className="flex items-center gap-0.5 text-xs text-muted-foreground shrink-0">
                  <Clock className="h-2.5 w-2.5" />
                  {timeAgo}
                </span>
              )}
            </div>
          )
        })}
      </div>
      <div className="p-2 border-t">
        <Button asChild size="sm" variant="outline" className="w-full h-7 text-xs">
          <Link to="/tasks">View All Tasks (All Agents)</Link>
        </Button>
      </div>
    </div>
  )
}
