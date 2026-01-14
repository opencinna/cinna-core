import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { EnvironmentsService } from "@/client"
import type { AgentEnvironmentPublic } from "@/client"
import { EnvironmentStatusBadge } from "./EnvironmentStatusBadge"
import { Play, Trash2, RefreshCw, Pause, Loader2, Wrench, MessageCircle } from "lucide-react"
import useCustomToast from "@/hooks/useCustomToast"

// Helper to get SDK display name
const getSDKDisplayName = (sdk: string | null | undefined): string => {
  if (!sdk || sdk === "claude-code/anthropic") return "Anthropic"
  if (sdk === "claude-code/minimax") return "MiniMax"
  return sdk
}

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

  const rebuildMutation = useMutation({
    mutationFn: () => EnvironmentsService.rebuildEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment rebuild started. This may take a few minutes.")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to rebuild environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const suspendMutation = useMutation({
    mutationFn: () => EnvironmentsService.suspendEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment suspended successfully")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to suspend environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
  })

  const handleDelete = () => {
    if (confirm("Delete this environment? This action cannot be undone.")) {
      deleteMutation.mutate()
    }
  }

  const handleRebuild = () => {
    if (
      confirm(
        "Rebuild this environment?\n\n" +
          "This will:\n" +
          "• Update core system files from the template\n" +
          "• Rebuild the Docker image\n" +
          "• Preserve all workspace data (scripts, files, credentials)\n\n" +
          "Continue?"
      )
    ) {
      rebuildMutation.mutate()
    }
  }

  const handleSuspend = () => {
    if (
      confirm(
        "Suspend this environment?\n\n" +
          "This will stop the container to save resources. " +
          "The environment will automatically reactivate when you send a message or open a session."
      )
    ) {
      suspendMutation.mutate()
    }
  }

  // Check if environment is in a transitional state (starting/activating)
  const isTransitioning = [
    "creating",
    "building",
    "initializing",
    "starting",
    "activating",
  ].includes(environment.status)

  return (
    <Card className={`p-4 ${environment.is_active ? "bg-green-50 dark:bg-green-950/20" : ""}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="text-lg font-semibold break-words">
              {environment.instance_name}
            <span className="text-sm text-muted-foreground pl-4">
              {environment.id}
            </span>
            </h3>
          </div>
          <div className="space-y-1 text-sm text-muted-foreground">
            <p>
              <span className="font-medium">Status:</span>{" "}
              <EnvironmentStatusBadge status={environment.status} />
            </p>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium">SDK:</span>
              <Badge variant="outline" className="text-xs gap-1">
                <MessageCircle className="h-3 w-3" />
                {getSDKDisplayName(environment.agent_sdk_conversation)}
              </Badge>
              <Badge variant="outline" className="text-xs gap-1">
                <Wrench className="h-3 w-3" />
                {getSDKDisplayName(environment.agent_sdk_building)}
              </Badge>
            </div>
            {environment.last_health_check && (
              <p>
                <span className="font-medium">Last health check:</span>{" "}
                {new Date(environment.last_health_check).toLocaleString()}
              </p>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-2">
          {(!environment.is_active || (environment.is_active && environment.status !== "running")) && (
            <Button
              size="sm"
              onClick={onActivate}
              className="gap-1"
              disabled={isTransitioning}
            >
              {isTransitioning ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Activating...
                </>
              ) : (
                <>
                  <Play className="h-4 w-4" />
                  Activate
                </>
              )}
            </Button>
          )}
          {environment.is_active && environment.status === "running" && (
            <Button
              size="sm"
              variant="outline"
              onClick={handleSuspend}
              disabled={suspendMutation.isPending}
              className="gap-1"
            >
              <Pause className="h-4 w-4" />
              Suspend
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={handleRebuild}
            disabled={
              rebuildMutation.isPending ||
              environment.status === "creating" ||
              environment.status === "building" ||
              environment.status === "rebuilding"
            }
            className="gap-1"
          >
            <RefreshCw className={`h-4 w-4 ${rebuildMutation.isPending ? "animate-spin" : ""}`} />
            Rebuild
          </Button>
          {!environment.is_active && (
            <Button
              size="sm"
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteMutation.isPending}
              className="gap-1"
            >
              <Trash2 className="h-4 w-4" />
              Delete
            </Button>
          )}
        </div>
      </div>
    </Card>
  )
}
