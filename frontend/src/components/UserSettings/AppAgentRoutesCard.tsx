import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import {
  Network,
  Copy,
  Check,
  HelpCircle,
  Wrench,
  MessageCircle,
} from "lucide-react"
import { useState } from "react"

import {
  UserAppAgentRoutesService,
  UtilsService,
  type SharedRoutePublic,
  type UserAppAgentRoutePublic,
} from "@/client"
import { GettingStartedModal } from "@/components/Onboarding/GettingStartedModal"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Switch } from "@/components/ui/switch"
import { Badge } from "@/components/ui/badge"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"



export function AppAgentRoutesCard() {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [detailRoute, setDetailRoute] = useState<SharedRoutePublic | null>(null)

  const { data: mcpInfo } = useQuery({
    queryKey: ["mcp-info"],
    queryFn: () => UtilsService.getMcpInfo(),
    staleTime: Infinity,
  })

  const appMcpUrl = mcpInfo?.mcp_server_url ?? ""

  const { data: routesData, isLoading } = useQuery({
    queryKey: ["user", "appAgentRoutes"],
    queryFn: () => UserAppAgentRoutesService.listUserAppAgentRoutes(),
  })

  const toggleSharedMutation = useMutation({
    mutationFn: ({ assignmentId, isEnabled }: { assignmentId: string; isEnabled: boolean }) =>
      UserAppAgentRoutesService.toggleAdminAssignment({
        assignmentId,
        isEnabled,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user", "appAgentRoutes"] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleCopyUrl = () => {
    navigator.clipboard.writeText(appMcpUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const sharedRoutes: SharedRoutePublic[] = routesData?.shared_routes ?? []

  // Personal routes — soft-deprecated, display-only with deprecation hint
  const personalRoutes: UserAppAgentRoutePublic[] = (routesData as any)?.personal_routes ?? []
  const hasPersonalRoutes = personalRoutes.length > 0

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Network className="h-4 w-4 text-blue-500" />
            MCP Server
          </CardTitle>
        </div>
        <CardDescription>
          Connect external MCP clients to the Application MCP Server
        </CardDescription>
        <div className="flex items-center gap-2 p-2 bg-muted rounded-md">
          {appMcpUrl ? (
            <>
              <code className="flex-1 text-xs truncate">{appMcpUrl}</code>
              <Button size="icon" variant="ghost" className="h-6 w-6 shrink-0" onClick={() => setShowHelp(true)}>
                <HelpCircle className="h-3 w-3" />
              </Button>
              <Button size="icon" variant="ghost" className="h-6 w-6 shrink-0" onClick={handleCopyUrl}>
                {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
              </Button>
            </>
          ) : (
            <span className="text-xs text-muted-foreground italic">
              MCP Server URL not configured
            </span>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-6">

        {/* MCP Shared Agents section */}
        <div className="space-y-2">
          <p className="text-sm font-medium">MCP Shared Agents</p>
          {isLoading ? (
            <p className="text-xs text-muted-foreground">Loading...</p>
          ) : sharedRoutes.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No agents shared with you. Agent owners can share their agents with you from the agent's Integrations tab.
            </p>
          ) : (
            <div className="space-y-2">
              {sharedRoutes.map((route) => (
                <div
                  key={route.assignment_id}
                  className="flex items-center justify-between p-2 border rounded-md"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      {route.session_mode === "building" ? (
                        <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                      ) : (
                        <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                      )}
                      <button
                        className="text-sm font-medium truncate text-left hover:underline cursor-pointer"
                        onClick={() => setDetailRoute(route)}
                      >
                        {route.agent_name}
                      </button>
                    </div>
                    {route.agent_owner_name && (
                      <p className="text-xs text-muted-foreground mt-0.5 ml-[22px]">
                        by {route.agent_owner_name}
                        {route.shared_by_name && route.shared_by_name !== route.agent_owner_name && (
                          <span className="ml-1">· shared by {route.shared_by_name}</span>
                        )}
                      </p>
                    )}
                    {!route.is_active && (
                      <p className="text-xs mt-0.5 ml-[22px]">
                        <span className="text-orange-500">Disabled by agent owner</span>
                      </p>
                    )}
                  </div>
                  <Switch
                    checked={route.is_enabled}
                    disabled={!route.is_active}
                    onCheckedChange={(v) =>
                      toggleSharedMutation.mutate({
                        assignmentId: route.assignment_id,
                        isEnabled: v,
                      })
                    }
                  />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Personal Routes — soft-deprecated, read-only display */}
        {hasPersonalRoutes && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <p className="text-sm font-medium text-muted-foreground">Personal Routes</p>
              <Badge variant="outline" className="text-xs">Legacy</Badge>
            </div>
            <p className="text-xs text-muted-foreground">
              Manage agent routes from each agent's Integrations tab.
            </p>
            <div className="space-y-2 opacity-70">
              {personalRoutes.map((route) => (
                <div
                  key={route.id}
                  className="flex items-center justify-between p-2 border rounded-md"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium truncate">{route.agent_name}</span>
                      <Badge variant="secondary" className="text-xs shrink-0">
                        {route.session_mode}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground truncate mt-0.5">
                      {route.trigger_prompt}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>

      {/* Shared route detail modal */}
      <Dialog open={!!detailRoute} onOpenChange={(open) => !open && setDetailRoute(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {detailRoute?.session_mode === "building" ? (
                <Wrench className="h-4 w-4 text-orange-500 shrink-0" />
              ) : (
                <MessageCircle className="h-4 w-4 text-blue-500 shrink-0" />
              )}
              {detailRoute?.agent_name}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            {detailRoute?.agent_owner_name && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-1">Owner</p>
                <p className="text-sm">{detailRoute.agent_owner_name}</p>
              </div>
            )}
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-1">Trigger Prompt</p>
              <p className="text-sm whitespace-pre-wrap bg-muted rounded-md p-2">
                {detailRoute?.trigger_prompt || "—"}
              </p>
            </div>
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-1">Message Patterns</p>
              {detailRoute?.message_patterns ? (
                <div className="space-y-1">
                  {detailRoute.message_patterns.split("\n").filter(Boolean).map((pattern, i) => (
                    <code
                      key={i}
                      className="block text-xs bg-muted rounded px-2 py-1"
                    >
                      {pattern}
                    </code>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No patterns configured</p>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <GettingStartedModal
        open={showHelp}
        onOpenChange={setShowHelp}
        initialArticle="app-mcp-setup"
      />
    </Card>
  )
}
