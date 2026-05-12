/**
 * McpConnectorsCardSimple — degraded view of the MCP Connectors card for
 * the ``agent-user`` role.
 *
 * Renders just the agent's auto-managed App MCP route (the one
 * ``InstallService._auto_create_app_mcp_route`` creates on install) with
 * a per-user enable/disable toggle. Hides every developer-tier
 * affordance:
 *
 *   - No "New" / "Add" dialog (no creating direct connectors, identity
 *     bindings, or extra App MCP routes)
 *   - No ``auto_enable_for_users`` superuser toggle
 *   - No user-share multi-select
 *   - Trigger prompt is a read-only mirror of the agent's
 *     ``router_trigger_prompt`` (editable via the Prompts tab modal)
 *
 * Toggling the row writes ``AppAgentRouteAssignment.is_enabled=false``
 * via ``UserAppAgentRoutesService.toggleAdminAssignment`` — the route
 * itself stays ``is_active=True`` so re-enable is a simple flip.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Unplug } from "lucide-react"

import {
  AgentAppMcpRoutesService,
  UserAppAgentRoutesService,
  type AppAgentRoutePublic,
} from "@/client"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"

interface McpConnectorsCardSimpleProps {
  agentId: string
}

export function McpConnectorsCardSimple({
  agentId,
}: McpConnectorsCardSimpleProps) {
  const { user: currentUser } = useAuth()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Single shared query key for the auto-managed route on this agent.
  // ``EditRouterTriggerPromptModal`` invalidates the same key after
  // saving so the trigger-prompt mirror refreshes without a manual
  // reload.
  const routesQueryKey = ["app-mcp-routes", agentId]

  const { data: routes = [], isLoading } = useQuery<AppAgentRoutePublic[]>({
    queryKey: routesQueryKey,
    queryFn: () =>
      AgentAppMcpRoutesService.listAgentAppMcpRoutes({ agentId }),
  })

  const autoRoute = routes.find((r) => r.is_auto_managed) ?? null
  const myAssignment = autoRoute?.assignments?.find(
    (a) => a.user_id === currentUser?.id,
  )

  const toggleMutation = useMutation({
    mutationFn: ({
      assignmentId,
      isEnabled,
    }: {
      assignmentId: string
      isEnabled: boolean
    }) =>
      UserAppAgentRoutesService.toggleAdminAssignment({
        assignmentId,
        isEnabled,
      }),
    onSuccess: () => {
      showSuccessToast("Updated MCP routing")
      queryClient.invalidateQueries({ queryKey: routesQueryKey })
    },
    onError: (error: unknown) => {
      const detail =
        (error as { body?: { detail?: string } })?.body?.detail ||
        (error as { message?: string })?.message ||
        "Failed to update toggle"
      showErrorToast(detail)
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Unplug className="h-5 w-5" />
          MCP Connectors
        </CardTitle>
        <CardDescription>
          Reach this agent from external MCP clients (e.g., Claude Desktop)
          via the shared App MCP Server.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : !autoRoute ? (
          <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
            <p>
              This install has no auto-routed App MCP entry yet. Set a{" "}
              <span className="font-medium">Trigger Prompt</span> for the
              agent on the Configuration tab — once it's set, the route will
              appear here automatically.
            </p>
          </div>
        ) : (
          <div className="rounded-md border p-4 space-y-3">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-1 min-w-0">
                <p className="font-medium text-sm">{autoRoute.name}</p>
                <p className="text-xs text-muted-foreground line-clamp-3">
                  {autoRoute.trigger_prompt}
                </p>
              </div>
              <Switch
                checked={!!myAssignment?.is_enabled}
                disabled={!myAssignment || toggleMutation.isPending}
                onCheckedChange={(checked) => {
                  if (!myAssignment) return
                  toggleMutation.mutate({
                    assignmentId: myAssignment.id,
                    isEnabled: checked,
                  })
                }}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Edit the trigger prompt for this install on the{" "}
              <span className="font-medium">Configuration</span> tab.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
