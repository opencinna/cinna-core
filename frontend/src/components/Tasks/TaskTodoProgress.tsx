import { cn } from "@/lib/utils"
import { CheckCircle2, Circle, Loader2 } from "lucide-react"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

export interface TodoItem {
  content: string
  activeForm: string
  status: "completed" | "in_progress" | "pending"
}

interface TaskTodoProgressProps {
  todos: TodoItem[]
  className?: string
}

export function TaskTodoProgress({ todos, className }: TaskTodoProgressProps) {
  if (!todos || todos.length === 0) return null

  const inProgress = todos.find((t) => t.status === "in_progress")

  return (
    <div className={cn("flex items-center gap-2", className)}>
      {/* Horizontal progress with connected circles */}
      <TooltipProvider delayDuration={200}>
        <div className="flex items-center">
          {todos.map((todo, i) => {
            const isLast = i === todos.length - 1

            const Icon =
              todo.status === "completed"
                ? CheckCircle2
                : todo.status === "in_progress"
                  ? Loader2
                  : Circle

            const iconColor =
              todo.status === "completed"
                ? "text-green-600 dark:text-green-400"
                : todo.status === "in_progress"
                  ? "text-blue-600 dark:text-blue-400"
                  : "text-muted-foreground/40"

            const lineColor =
              todo.status === "completed"
                ? "bg-green-600 dark:bg-green-400"
                : "bg-muted-foreground/20"

            return (
              <div key={i} className="flex items-center">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="cursor-default">
                      <Icon
                        className={cn(
                          "h-3 w-3 flex-shrink-0",
                          iconColor,
                          todo.status === "in_progress" && "animate-spin"
                        )}
                      />
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-xs">
                    <p className="text-xs">{todo.content}</p>
                  </TooltipContent>
                </Tooltip>
                {/* Connector line */}
                {!isLast && <div className={cn("h-0.5 w-2 mx-0.5", lineColor)} />}
              </div>
            )
          })}
        </div>
      </TooltipProvider>

      {/* Current step text hint - inline */}
      {inProgress && (
        <span className="text-muted-foreground truncate max-w-[150px]">
          {inProgress.activeForm}
        </span>
      )}
    </div>
  )
}
