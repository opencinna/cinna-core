/**
 * McpConnectorsCardSimple — degraded view of the MCP Connectors card for
 * the ``agent-user`` role.
 *
 * Renders just the agent's auto-managed App MCP route (the one
 * ``InstallService._auto_create_app_mcp_route`` creates on install, or
 * ``AppAgentRouteService._create_auto_route_for_agent`` backfills when the
 * owner sets a trigger prompt later) with a per-user enable/disable
 * toggle. Hides every developer-tier affordance:
 *
 *   - No "New" / "Add" dialog (no creating direct connectors, identity
 *     bindings, or extra App MCP routes)
 *   - No ``auto_enable_for_users`` superuser toggle
 *   - No user-share multi-select
 *   - Trigger prompt is a read-only mirror of the agent's
 *     ``router_trigger_prompt`` (editable via the Prompts tab modal)
 *
 * The copy carries the whole feature explanation, because this card is an
 * ``agent-user``'s only exposure to App MCP routing — they never see the
 * developer card's creation dialog, so nothing else tells them what the
 * switch governs. Three things have to be legible without docs:
 *
 *   1. What the switch turns on (reachability from external MCP clients),
 *   2. What it does NOT affect (chat here, schedules, email, webhooks),
 *   3. What the trigger prompt is for (the router picks between the
 *      user's agents with it).
 *
 * Toggling the row writes ``AppAgentRouteAssignment.is_enabled=false``
 * via ``UserAppAgentRoutesService.toggleAdminAssignment`` — the route
 * itself stays ``is_active=True`` so re-enable is a simple flip.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
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
import { Separator } from "@/components/ui/separator"
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
  const isEnabled = !!myAssignment?.is_enabled

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
          Use this agent from apps outside Cinna — Claude Desktop, Cursor, or
          any MCP client connected to your personal MCP Server URL.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : !autoRoute ? (
          <div className="rounded-md border border-dashed p-4 space-y-2 text-sm">
            <p className="font-medium">Not available in MCP clients yet</p>
            <p className="text-xs text-muted-foreground">
              This agent needs a <span className="font-medium">Trigger
              Prompt</span> — one sentence describing when it should be used —
              before an MCP client can route anything to it. Set one on the{" "}
              <span className="font-medium">Configuration</span> tab and it
              will show up here.
            </p>
          </div>
        ) : (
          <div className="rounded-md border divide-y">
            {/* ── What the switch actually governs ─────────────────── */}
            <div className="p-4 space-y-1">
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-1 min-w-0">
                  <p className="font-medium text-sm">
                    Available in external MCP clients
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {isEnabled
                      ? "Your MCP client can send messages to this agent."
                      : "Your MCP client will not see or use this agent."}
                  </p>
                </div>
                <Switch
                  checked={isEnabled}
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
              {!myAssignment && (
                <p className="text-xs text-amber-600 dark:text-amber-500">
                  This agent is routed for someone else, so there is nothing
                  for you to turn on here.
                </p>
              )}
            </div>

            {/* ── The trigger prompt, explained ─────────────────────── */}
            <div className="p-4 space-y-2">
              <p className="text-sm font-medium">When this agent gets picked</p>
              <p className="text-xs text-muted-foreground">
                One MCP Server URL reaches all of your agents. This sentence is
                how it decides that a message belongs to this one:
              </p>
              <blockquote className="border-l-2 pl-3 text-xs italic text-muted-foreground line-clamp-4">
                {autoRoute.trigger_prompt}
              </blockquote>
              <p className="text-xs text-muted-foreground">
                This sentence is the agent's{" "}
                <span className="font-medium text-foreground">
                  Trigger Prompt
                </span>{" "}
                — edit it on the{" "}
                <span className="font-medium">Configuration</span> tab.
              </p>
            </div>
          </div>
        )}

        <Separator className="my-4" />

        <p className="text-xs text-muted-foreground">
          Turning this off only hides the agent from MCP clients — chat here,
          schedules, and other integrations keep working. Your MCP Server URL
          and setup steps live in{" "}
          <Link
            to="/settings"
            hash="channels"
            className="font-medium underline underline-offset-2 hover:text-foreground"
          >
            Settings → Channels
          </Link>
          .
        </p>
      </CardContent>
    </Card>
  )
}
