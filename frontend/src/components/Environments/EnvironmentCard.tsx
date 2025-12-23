import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { EnvironmentsService } from "@/client"
import type { AgentEnvironmentPublic } from "@/client"
import { EnvironmentStatusBadge } from "./EnvironmentStatusBadge"
import { Badge } from "@/components/ui/badge"
import { CheckCircle2, Play, Trash2 } from "lucide-react"
import useCustomToast from "@/hooks/useCustomToast"

interface EnvironmentCardProps {
  environment: AgentEnvironmentPublic
  agentId: string
  onActivate?: () => void
}

export function EnvironmentCard({ environment, agentId, onActivate }: EnvironmentCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const deleteMutation = useMutation({
    mutationFn: () => EnvironmentsService.deleteEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment has been deleted")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const handleDelete = () => {
    if (confirm("Delete this environment? This action cannot be undone.")) {
      deleteMutation.mutate()
    }
  }

  return (
    <Card className={`p-4 ${environment.is_active ? "bg-green-50 dark:bg-green-950/20" : ""}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="text-lg font-semibold break-words">
              {environment.env_name === "python-env-advanced"
                ? "Python Advanced"
                : environment.env_name}
            </h3>
            {environment.is_active && (
              <Badge variant="default" className="gap-1">
                <CheckCircle2 className="h-3 w-3" />
                Active
              </Badge>
            )}
          </div>
          <div className="space-y-1 text-sm text-muted-foreground">
            <p>
              <span className="font-medium">Status:</span>{" "}
              <EnvironmentStatusBadge status={environment.status} />
            </p>
            {environment.last_health_check && (
              <p>
                <span className="font-medium">Last health check:</span>{" "}
                {new Date(environment.last_health_check).toLocaleString()}
              </p>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-2">
          {!environment.is_active && (
            <Button size="sm" onClick={onActivate} className="gap-1">
              <Play className="h-4 w-4" />
              Activate
            </Button>
          )}
          <Button
            size="sm"
            variant="destructive"
            onClick={handleDelete}
            disabled={deleteMutation.isPending || environment.is_active}
            className="gap-1"
          >
            <Trash2 className="h-4 w-4" />
            Delete
          </Button>
        </div>
      </div>
    </Card>
  )
}
