import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { EnvironmentsService } from "@/client"
import type { AgentEnvironmentPublic } from "@/client"
import { EnvironmentStatusBadge } from "./EnvironmentStatusBadge"
import { Badge } from "@/components/ui/badge"
import { CheckCircle2, Play, Square, Trash2 } from "lucide-react"
import useCustomToast from "@/hooks/useCustomToast"

interface EnvironmentCardProps {
  environment: AgentEnvironmentPublic
  agentId: string
  onActivate?: () => void
}

export function EnvironmentCard({ environment, agentId, onActivate }: EnvironmentCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const startMutation = useMutation({
    mutationFn: () => EnvironmentsService.startEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast(`${environment.instance_name} is starting...`)
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to start environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const stopMutation = useMutation({
    mutationFn: () => EnvironmentsService.stopEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast(`${environment.instance_name} is stopping...`)
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to stop environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => EnvironmentsService.deleteEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast(`${environment.instance_name} has been deleted`)
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const handleStart = () => {
    if (confirm(`Start environment "${environment.instance_name}"?`)) {
      startMutation.mutate()
    }
  }

  const handleStop = () => {
    if (confirm(`Stop environment "${environment.instance_name}"?`)) {
      stopMutation.mutate()
    }
  }

  const handleDelete = () => {
    if (
      confirm(
        `Delete environment "${environment.instance_name}"? This action cannot be undone.`
      )
    ) {
      deleteMutation.mutate()
    }
  }

  return (
    <Card className={`p-4 ${environment.is_active ? "bg-green-50 dark:bg-green-950/20" : ""}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="text-lg font-semibold break-words">{environment.instance_name}</h3>
            {environment.is_active && (
              <Badge variant="default" className="gap-1">
                <CheckCircle2 className="h-3 w-3" />
                Active
              </Badge>
            )}
          </div>
          <div className="space-y-1 text-sm text-muted-foreground">
            <p>
              <span className="font-medium">Environment:</span> {environment.env_name} v
              {environment.env_version}
            </p>
            <p>
              <span className="font-medium">Type:</span> {environment.type}
            </p>
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
          {environment.status === "stopped" && (
            <Button
              size="sm"
              onClick={handleStart}
              disabled={startMutation.isPending}
              className="gap-1"
            >
              <Play className="h-4 w-4" />
              Start
            </Button>
          )}
          {environment.status === "running" && (
            <Button
              size="sm"
              variant="outline"
              onClick={handleStop}
              disabled={stopMutation.isPending}
              className="gap-1"
            >
              <Square className="h-4 w-4" />
              Stop
            </Button>
          )}
          {!environment.is_active && environment.status === "running" && (
            <Button size="sm" variant="secondary" onClick={onActivate}>
              Set Active
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
