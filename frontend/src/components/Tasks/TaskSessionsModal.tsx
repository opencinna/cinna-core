import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { TasksService } from "@/client"
import type { SessionPublic } from "@/client"
import { Clock, Wrench, MessageCircle, Loader2 } from "lucide-react"
import { RelativeTime } from "@/components/Common/RelativeTime"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

interface TaskSessionsModalProps {
  taskId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function TaskSessionsModal({
  taskId,
  open,
  onOpenChange,
}: TaskSessionsModalProps) {
  const navigate = useNavigate()

  const { data, isLoading } = useQuery({
    queryKey: ["task-sessions", taskId],
    queryFn: () => TasksService.listTaskSessions({ id: taskId }),
    enabled: open,
  })

  const sessions = data?.data || []

  const handleSessionClick = (sessionId: string) => {
    onOpenChange(false)
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
      search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Task Sessions</DialogTitle>
        </DialogHeader>
        <div className="py-2">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">
              No sessions found for this task
            </p>
          ) : (
            <div className="space-y-0.5 max-h-[400px] overflow-y-auto">
              {sessions.map((session: SessionPublic) => (
                <button
                  key={session.id}
                  onClick={() => handleSessionClick(session.id)}
                  className="w-full text-left px-3 py-2 rounded-md hover:bg-gradient-to-r hover:from-accent/50 hover:to-accent/30 transition-all group"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 min-w-0">
                        {session.mode === "building" ? (
                          <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                        ) : (
                          <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                        )}
                        <p className="text-sm text-foreground truncate min-w-0">
                          {session.title || "Untitled Session"}
                        </p>
                        <span
                          className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
                            session.status === "completed"
                              ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                              : session.status === "error"
                                ? "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400"
                                : "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                          }`}
                        >
                          {session.status}
                        </span>
                      </div>
                    </div>

                    <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
                      <Clock className="h-3 w-3" />
                      <RelativeTime
                        timestamp={session.last_message_at || session.created_at}
                      />
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
