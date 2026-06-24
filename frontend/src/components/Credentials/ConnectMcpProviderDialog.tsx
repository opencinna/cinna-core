import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Bot, Plug, Search, Server } from "lucide-react"
import { useMemo, useState } from "react"

import {
  type MCPProviderConnectionResponse,
  McpProvidersService,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { openMcpProviderOAuthPopup } from "@/utils/mcpProviderOAuth"

interface ConnectMcpProviderDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Optional consumer agent to link the new credential to immediately. */
  defaultConsumerAgentId?: string
  /**
   * Active workspace to stamp a new *external* provider with when there is no
   * consumer agent (so a manual external MCP server lands in the user's current
   * workspace, like any "My Credentials" entry). Ignored for the platform path
   * (that workspace follows the producer/consumer agents).
   */
  defaultWorkspaceId?: string
  /** Called after a successful connect (e.g. to refresh the parent list). */
  onConnected?: () => void
}

type Flow = "select" | "platform" | "external"
type ExternalAuthMode = "none" | "fixed_token" | "oauth_dcr"

/**
 * "Connect MCP Provider" — create an ``mcp_provider`` connection credential that
 * the consumer agent's SDK receives as a first-class MCP server (not a
 * credentials.json entry). Two flows:
 *   (a) connect to another platform agent's agent-to-agent MCP connector, or
 *   (b) add an arbitrary external MCP server (none / fixed-token / OAuth-DCR).
 * For OAuth/DCR the backend returns an authorize URL which we open in a popup
 * (mirroring the Google credential OAuth callback) to complete authorization.
 */
export function ConnectMcpProviderDialog({
  open,
  onOpenChange,
  defaultConsumerAgentId,
  defaultWorkspaceId,
  onConnected,
}: ConnectMcpProviderDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [flow, setFlow] = useState<Flow>("select")
  const [agentSearch, setAgentSearch] = useState("")

  // External form state
  const [endpointUrl, setEndpointUrl] = useState("")
  const [transport, setTransport] = useState("streamable-http")
  const [authMode, setAuthMode] = useState<ExternalAuthMode>("none")
  const [token, setToken] = useState("")
  const [label, setLabel] = useState("")
  const [modeConversation, setModeConversation] = useState(true)
  const [modeBuilding, setModeBuilding] = useState(true)

  const reset = () => {
    setFlow("select")
    setAgentSearch("")
    setEndpointUrl("")
    setTransport("streamable-http")
    setAuthMode("none")
    setToken("")
    setLabel("")
    setModeConversation(true)
    setModeBuilding(true)
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) reset()
    onOpenChange(next)
  }

  const { data: discoverable, isLoading: discoverableLoading } = useQuery({
    queryKey: ["mcp-providers", "discoverable-agents", defaultConsumerAgentId],
    queryFn: () =>
      McpProvidersService.listDiscoverableAgents({
        consumerAgentId: defaultConsumerAgentId,
      }),
    enabled: open && flow === "platform",
  })

  const filteredAgents = useMemo(() => {
    const items = discoverable?.data ?? []
    const q = agentSearch.trim().toLowerCase()
    if (!q) return items
    return items.filter(
      (a) =>
        a.agent_name.toLowerCase().includes(q) ||
        a.connector_name.toLowerCase().includes(q),
    )
  }, [discoverable, agentSearch])

  const afterConnect = (data: MCPProviderConnectionResponse) => {
    queryClient.invalidateQueries({ queryKey: ["credentials"] })
    if (defaultConsumerAgentId) {
      queryClient.invalidateQueries({
        queryKey: ["agent-credentials", defaultConsumerAgentId],
      })
    }
    onConnected?.()

    // OAuth/DCR: open the authorize popup so the user can complete consent.
    // The popup posts a message back; we navigate to the credential detail.
    if (data.authorize_url) {
      openMcpProviderOAuthPopup({
        authorizeUrl: data.authorize_url,
        onSuccess: () => {
          showSuccessToast("MCP server authorized")
          queryClient.invalidateQueries({ queryKey: ["credentials"] })
          queryClient.invalidateQueries({
            queryKey: ["mcp-provider-status", data.credential_id],
          })
        },
        onError: (msg) => showErrorToast(msg || "Authorization failed"),
      })
    } else {
      showSuccessToast("Connected — credential created")
    }

    handleOpenChange(false)
    navigate({
      to: "/credential/$credentialId",
      params: { credentialId: data.credential_id },
    })
  }

  const connectAgentMutation = useMutation({
    mutationFn: (connectorId: string) =>
      McpProvidersService.connectAgent({
        requestBody: {
          connector_id: connectorId,
          consumer_agent_id: defaultConsumerAgentId ?? null,
        },
      }),
    onSuccess: afterConnect,
    onError: (err) => handleError.bind(showErrorToast)(err as any),
  })

  const connectExternalMutation = useMutation({
    mutationFn: () =>
      McpProvidersService.connectExternal({
        requestBody: {
          endpoint_url: endpointUrl.trim(),
          transport,
          auth_mode: authMode,
          token: authMode === "fixed_token" ? token.trim() : null,
          consumer_agent_id: defaultConsumerAgentId ?? null,
          // Only relevant when there is no consumer agent — a manual external
          // provider follows the user's active workspace (backend ignores it
          // when a consumer agent is supplied).
          user_workspace_id: defaultConsumerAgentId
            ? null
            : (defaultWorkspaceId ?? null),
          label: label.trim() || null,
          mcp_mode_conversation: modeConversation,
          mcp_mode_building: modeBuilding,
        },
      }),
    onSuccess: afterConnect,
    onError: (err) => handleError.bind(showErrorToast)(err as any),
  })

  const externalValid =
    endpointUrl.trim().length > 0 &&
    (modeConversation || modeBuilding) &&
    (authMode !== "fixed_token" || token.trim().length > 0)

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {flow === "select" && (
          <>
            <DialogHeader>
              <DialogTitle>Connect MCP Provider</DialogTitle>
              <DialogDescription>
                Give this agent access to another MCP server. The connection is
                injected into the agent's SDK as a first-class MCP server.
              </DialogDescription>
            </DialogHeader>
            <div className="grid grid-cols-1 gap-3 py-2">
              <button
                type="button"
                onClick={() => setFlow("platform")}
                className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
              >
                <div className="flex items-center gap-2">
                  <Bot className="h-5 w-5 text-primary" />
                  <span className="font-medium text-sm">Platform Agent</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  Connect to another platform agent that exposes an
                  agent-to-agent MCP connector you're allowed to use.
                </p>
              </button>
              <button
                type="button"
                onClick={() => setFlow("external")}
                className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
              >
                <div className="flex items-center gap-2">
                  <Server className="h-5 w-5 text-blue-500" />
                  <span className="font-medium text-sm">
                    External MCP Server
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  Add an arbitrary remote MCP server by URL — no auth, a fixed
                  token, or OAuth (Dynamic Client Registration).
                </p>
              </button>
            </div>
          </>
        )}

        {/* (Fix 3) The Platform-Agent path intentionally has NO endpoint-URL
            field: the MCP server URL is derived server-side from the chosen
            connector ({MCP_SERVER_BASE_URL}/{connector_id}/mcp). Do not
            reintroduce a URL input here — URL entry belongs only to the
            `flow === "external"` block below. */}
        {flow === "platform" && (
          <>
            <DialogHeader>
              <DialogTitle>Connect to a Platform Agent</DialogTitle>
              <DialogDescription>
                <button
                  type="button"
                  onClick={() => setFlow("select")}
                  className="text-primary hover:underline text-sm"
                >
                  &larr; Back
                </button>
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-3">
              <div className="relative">
                <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
                <Input
                  autoFocus
                  placeholder="Search agents..."
                  value={agentSearch}
                  onChange={(e) => setAgentSearch(e.target.value)}
                  className="pl-9"
                />
              </div>
              <div className="flex flex-wrap gap-2 max-h-[300px] overflow-y-auto">
                {discoverableLoading ? (
                  <p className="text-sm text-muted-foreground py-2">Loading…</p>
                ) : filteredAgents.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-2">
                    {(discoverable?.data?.length ?? 0) === 0
                      ? "No agents expose an agent-to-agent MCP connector you can use."
                      : "No agents match your search."}
                  </p>
                ) : (
                  filteredAgents.map((agent) => {
                    const preset = getColorPreset(agent.ui_color_preset)
                    return (
                      <button
                        key={agent.connector_id}
                        type="button"
                        disabled={connectAgentMutation.isPending}
                        onClick={() =>
                          connectAgentMutation.mutate(agent.connector_id)
                        }
                        title={`${agent.connector_name} (${agent.mode})`}
                        className={cn(
                          "cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2 disabled:opacity-50",
                          preset.badgeBg,
                          preset.badgeText,
                          preset.badgeHover,
                        )}
                      >
                        <Bot className="h-4 w-4" />
                        {agent.agent_name}
                      </button>
                    )
                  })
                )}
              </div>
            </div>
          </>
        )}

        {flow === "external" && (
          <>
            <DialogHeader>
              <DialogTitle>Add External MCP Server</DialogTitle>
              <DialogDescription>
                <button
                  type="button"
                  onClick={() => setFlow("select")}
                  className="text-primary hover:underline text-sm"
                >
                  &larr; Back
                </button>
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 max-h-[65vh] overflow-y-auto pr-1">
              <div className="space-y-2">
                <Label htmlFor="mcp-endpoint">Endpoint URL</Label>
                <Input
                  id="mcp-endpoint"
                  placeholder="https://mcp.example.com/mcp"
                  value={endpointUrl}
                  onChange={(e) => setEndpointUrl(e.target.value)}
                  className="font-mono text-sm"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="mcp-label">Name (optional)</Label>
                <Input
                  id="mcp-label"
                  placeholder="My MCP Server"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label>Transport</Label>
                <Select value={transport} onValueChange={setTransport}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="streamable-http">
                      Streamable HTTP
                    </SelectItem>
                    <SelectItem value="sse">SSE</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Authentication</Label>
                <Select
                  value={authMode}
                  onValueChange={(v) => setAuthMode(v as ExternalAuthMode)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    <SelectItem value="fixed_token">Fixed token</SelectItem>
                    <SelectItem value="oauth_dcr">OAuth (DCR)</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {authMode === "none" &&
                    "The server requires no authorization header."}
                  {authMode === "fixed_token" &&
                    "We send the token you provide as a Bearer header. Use this for static API keys."}
                  {authMode === "oauth_dcr" &&
                    "We register a client with the server (Dynamic Client Registration) and run the authorization flow in a popup. Refresh is handled for you."}
                </p>
              </div>
              {authMode === "fixed_token" && (
                <div className="space-y-2">
                  <Label htmlFor="mcp-token">Token</Label>
                  <Input
                    id="mcp-token"
                    type="password"
                    placeholder="Bearer token"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    className="font-mono text-sm"
                  />
                </div>
              )}
              <div className="space-y-2">
                <Label>Apply to modes</Label>
                <div className="flex items-center gap-6">
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={modeConversation}
                      onCheckedChange={(c) => setModeConversation(c === true)}
                    />
                    Conversation
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={modeBuilding}
                      onCheckedChange={(c) => setModeBuilding(c === true)}
                    />
                    Building
                  </label>
                </div>
                {!modeConversation && !modeBuilding && (
                  <p className="text-xs text-destructive">
                    Enable at least one mode or the provider will be inert.
                  </p>
                )}
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setFlow("select")}>
                Back
              </Button>
              <LoadingButton
                onClick={() => connectExternalMutation.mutate()}
                loading={connectExternalMutation.isPending}
                disabled={!externalValid}
              >
                <Plug className="h-4 w-4 mr-1" />
                {authMode === "oauth_dcr" ? "Connect & Authorize" : "Connect"}
              </LoadingButton>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
