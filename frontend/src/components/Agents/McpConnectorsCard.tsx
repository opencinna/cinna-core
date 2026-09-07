import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Bot, Check, Copy, Plus, Unplug, Users } from "lucide-react"
import { useState } from "react"
import { McpConnectorRow } from "@/components/Agents/McpConnectorRow"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
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
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import useRole from "@/hooks/useRole"
import { McpDirectTokensManager } from "./McpDirectTokensManager"

const API_BASE = import.meta.env.VITE_API_URL || ""

function getAuthHeaders() {
  const token = localStorage.getItem("access_token")
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface McpConnectorAllowedUser {
  id: string
  email: string
  full_name: string | null
}

interface McpConnector {
  id: string
  agent_id: string
  owner_id: string
  name: string
  mode: string
  is_active: boolean
  is_agent_to_agent: boolean
  allowed_emails: string[]
  allowed_user_ids: string[]
  allowed_users: McpConnectorAllowedUser[]
  allow_token_access: boolean
  max_clients: number
  mcp_server_url: string | null
  created_at: string
  updated_at: string
}

type CreateStep = "type_select" | "form"
type CreateType = "direct" | "agent2agent"

interface McpConnectorsCardProps {
  agentId: string
  agentName: string
}

// ---------------------------------------------------------------------------
// McpConnectorsCard
// ---------------------------------------------------------------------------

export function McpConnectorsCard({
  agentId,
  agentName,
}: McpConnectorsCardProps) {
  // RD-7: creating an agent-to-agent connector exposes an agent over MCP, so it
  // requires agent-developer. Consuming a connection is use-only (agent-user).
  const { isDeveloper } = useRole()

  // ---- Create dialog state ----
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [createStep, setCreateStep] = useState<CreateStep>("type_select")
  const [createType, setCreateType] = useState<CreateType>("direct")

  // Direct connector form
  const [name, setName] = useState("")
  const [mode, setMode] = useState("conversation")
  const [allowedUsers, setAllowedUsers] = useState<UserAllowlistSelectedItem[]>(
    [],
  )
  const [allowTokenAccess, setAllowTokenAccess] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<McpConnector | null>(null)

  // Agent-to-agent connector form (exposes this agent over MCP for other agents)
  const [a2aName, setA2aName] = useState("")
  const [a2aMode, setA2aMode] = useState("conversation")
  const [a2aAllowedUsers, setA2aAllowedUsers] = useState<
    UserAllowlistSelectedItem[]
  >([])

  // Edit connector state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingConnector, setEditingConnector] = useState<McpConnector | null>(
    null,
  )
  const [editName, setEditName] = useState("")
  const [editMode, setEditMode] = useState("conversation")
  const [editAllowedUsers, setEditAllowedUsers] = useState<
    UserAllowlistSelectedItem[]
  >([])
  const [editAllowTokenAccess, setEditAllowTokenAccess] = useState(false)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // ---- Queries ----

  const { data: connectorData, isLoading: isLoadingConnectors } = useQuery<{
    data: McpConnector[]
    count: number
    mcp_server_base_url: string | null
  }>({
    queryKey: ["mcp-connectors", agentId],
    queryFn: async () => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors`,
        {
          headers: getAuthHeaders(),
        },
      )
      if (!res.ok) throw new Error("Failed to load connectors")
      return res.json()
    },
  })

  const allConnectors = connectorData?.data ?? []
  // agent-to-agent connectors get their own sub-section; the rest are the
  // existing external-client (direct) connectors.
  const connectors = allConnectors.filter((c) => !c.is_agent_to_agent)
  const a2aConnectors = allConnectors.filter((c) => c.is_agent_to_agent)
  const mcpServerBaseUrl = connectorData?.mcp_server_base_url ?? null

  const getMcpServerUrl = (connectorId: string) =>
    mcpServerBaseUrl ? `${mcpServerBaseUrl}/${connectorId}/mcp` : null

  // ---- Mutations: Direct Connectors ----

  const createConnectorMutation = useMutation({
    mutationFn: async (body: {
      name: string
      mode: string
      allowed_user_ids: string[]
      allow_token_access: boolean
      is_agent_to_agent?: boolean
    }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors`,
        {
          method: "POST",
          headers: getAuthHeaders(),
          body: JSON.stringify(body),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(
          (err as { detail?: string }).detail || "Failed to create connector",
        )
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("MCP connector created")
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
      handleDialogClose(false)
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const deleteConnectorMutation = useMutation({
    mutationFn: async (connectorId: string) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors/${connectorId}`,
        { method: "DELETE", headers: getAuthHeaders() },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(
          (err as { detail?: string }).detail || "Failed to delete connector",
        )
      }
    },
    onSuccess: () => {
      showSuccessToast("Connector deleted")
      setDeleteTarget(null)
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const toggleConnectorMutation = useMutation({
    mutationFn: async ({
      connectorId,
      isActive,
    }: {
      connectorId: string
      isActive: boolean
    }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors/${connectorId}`,
        {
          method: "PUT",
          headers: getAuthHeaders(),
          body: JSON.stringify({ is_active: isActive }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(
          (err as { detail?: string }).detail || "Failed to update connector",
        )
      }
      return res.json()
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const updateConnectorMutation = useMutation({
    mutationFn: async ({
      connectorId,
      body,
    }: {
      connectorId: string
      body: {
        name?: string
        mode?: string
        allowed_user_ids?: string[]
        allow_token_access?: boolean
      }
    }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors/${connectorId}`,
        {
          method: "PUT",
          headers: getAuthHeaders(),
          body: JSON.stringify(body),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(
          (err as { detail?: string }).detail || "Failed to update connector",
        )
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("Connector updated")
      setEditDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  // ---- Handlers ----

  const handleDialogClose = (open: boolean) => {
    setCreateDialogOpen(open)
    if (!open) {
      setCreateStep("type_select")
      setCreateType("direct")
      setName("")
      setMode("conversation")
      setAllowedUsers([])
      setAllowTokenAccess(false)
      setA2aName("")
      setA2aMode("conversation")
      setA2aAllowedUsers([])
    }
  }

  const handleTypeSelect = (type: CreateType) => {
    setCreateType(type)
    setCreateStep("form")
    if (type === "agent2agent" && !a2aName) {
      setA2aName(`${agentName} (Agent to Agent)`)
    }
  }

  const handleCreateConnector = () => {
    createConnectorMutation.mutate({
      name,
      mode,
      allowed_user_ids: allowedUsers.map((s) => s.userId),
      allow_token_access: allowTokenAccess,
    })
  }

  const handleCreateAgent2Agent = () => {
    // Agent-to-agent connectors always allow token access — the consumer's
    // "Connect MCP Provider" flow mints a connector-scoped direct token to
    // authenticate the cross-agent MCP calls.
    createConnectorMutation.mutate({
      name: a2aName,
      mode: a2aMode,
      allowed_user_ids: a2aAllowedUsers.map((s) => s.userId),
      allow_token_access: true,
      is_agent_to_agent: true,
    })
  }

  const handleCopyUrl = async (url: string, id: string) => {
    try {
      await navigator.clipboard.writeText(url)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      showErrorToast("Failed to copy URL")
    }
  }

  const handleEditConnectorOpen = (connector: McpConnector) => {
    setEditingConnector(connector)
    setEditName(connector.name)
    setEditMode(connector.mode)
    // Seed the picker from the resolved allowed_users projection so pills show
    // names/emails rather than raw UUIDs.
    setEditAllowedUsers(
      (connector.allowed_user_ids || []).map((uid) => {
        const resolved = (connector.allowed_users || []).find(
          (u) => u.id === uid,
        )
        return {
          id: uid,
          userId: uid,
          fallbackLabel: resolved?.full_name || resolved?.email || uid,
        }
      }),
    )
    setEditAllowTokenAccess(connector.allow_token_access)
    setEditDialogOpen(true)
  }

  const handleEditConnectorSave = () => {
    if (!editingConnector) return
    const body: {
      name?: string
      mode?: string
      allowed_user_ids?: string[]
      allow_token_access?: boolean
    } = {}
    if (editName !== editingConnector.name) body.name = editName
    if (editMode !== editingConnector.mode) body.mode = editMode
    const newUserIds = editAllowedUsers.map((s) => s.userId)
    if (
      JSON.stringify(newUserIds) !==
      JSON.stringify(editingConnector.allowed_user_ids || [])
    ) {
      body.allowed_user_ids = newUserIds
    }
    // The "Allow token access" switch is hidden for agent2agent connectors,
    // where it must stay auto-enabled — never send the now-hidden state for them.
    if (
      !editingConnector.is_agent_to_agent &&
      editAllowTokenAccess !== editingConnector.allow_token_access
    ) {
      body.allow_token_access = editAllowTokenAccess
    }
    updateConnectorMutation.mutate({ connectorId: editingConnector.id, body })
  }

  const isLoading = isLoadingConnectors

  // ---- Render ----

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Unplug className="h-5 w-5" />
              MCP Connectors
            </CardTitle>
            <CardDescription>
              Connect external MCP clients (Claude Desktop, Cursor) to this
              agent
            </CardDescription>
          </div>
          <Dialog open={createDialogOpen} onOpenChange={handleDialogClose}>
            <DialogTrigger asChild>
              <Button size="sm">
                <Plus className="h-4 w-4 mr-1" />
                New
              </Button>
            </DialogTrigger>

            <DialogContent>
              {createStep === "type_select" ? (
                <>
                  <DialogHeader>
                    <DialogTitle>Add MCP Integration</DialogTitle>
                    <DialogDescription>
                      Choose how to connect this agent to MCP clients.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="grid grid-cols-1 gap-3 py-2">
                    <button
                      onClick={() => handleTypeSelect("direct")}
                      className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                    >
                      <div className="flex items-center gap-2">
                        <Unplug className="h-5 w-5 text-primary" />
                        <span className="font-medium text-sm">
                          Direct MCP Connector
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Dedicated MCP endpoint for this agent. External clients
                        connect directly to this specific agent.
                      </p>
                    </button>

                    {isDeveloper && (
                      <button
                        onClick={() => handleTypeSelect("agent2agent")}
                        className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                      >
                        <div className="flex items-center gap-2">
                          <Bot className="h-5 w-5 text-emerald-500" />
                          <span className="font-medium text-sm">
                            Agent to Agent MCP Connector
                          </span>
                        </div>
                        <p className="text-xs text-muted-foreground">
                          Expose this agent over MCP so other platform agents
                          can connect to it via "Connect MCP Provider". A direct
                          token is minted automatically; control who may consume
                          it.
                        </p>
                      </button>
                    )}
                  </div>
                </>
              ) : createType === "direct" ? (
                <>
                  <DialogHeader>
                    <DialogTitle>Create MCP Connector</DialogTitle>
                    <DialogDescription>
                      <button
                        onClick={() => setCreateStep("type_select")}
                        className="text-primary hover:underline text-sm"
                      >
                        &larr; Back
                      </button>
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="connector-name">Name</Label>
                      <Input
                        id="connector-name"
                        placeholder="My Connector"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Mode</Label>
                      <Select value={mode} onValueChange={setMode}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="conversation">
                            Conversation
                          </SelectItem>
                          <SelectItem value="building">Building</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Conversation mode for chat interactions, Building mode
                        for development tasks.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <UserAllowlistPicker
                        enabled={
                          createDialogOpen &&
                          createStep === "form" &&
                          createType === "direct"
                        }
                        selected={allowedUsers}
                        onAdd={(u) =>
                          setAllowedUsers((prev) =>
                            prev.some((s) => s.userId === u.id)
                              ? prev
                              : [
                                  ...prev,
                                  {
                                    id: u.id,
                                    userId: u.id,
                                    fallbackLabel: u.full_name || u.email,
                                  },
                                ],
                          )
                        }
                        onRemove={(item) =>
                          setAllowedUsers((prev) =>
                            prev.filter((s) => s.userId !== item.userId),
                          )
                        }
                        label={
                          <Label className="flex items-center gap-2">
                            <Users className="h-4 w-4" />
                            Allowed Users (optional)
                          </Label>
                        }
                        searchPlaceholder="Search users..."
                        emptyHint="Leave empty for owner-only access."
                      />
                    </div>
                    <Separator />
                    <div className="flex items-center justify-between py-1">
                      <div className="space-y-0.5 pr-4">
                        <Label className="text-sm">Allow token access</Label>
                        <p className="text-xs text-muted-foreground">
                          When off, clients must authorize via OAuth. When on,
                          you can generate a direct access token that a client
                          uses without an account — it connects under your name,
                          for this connector only.
                        </p>
                      </div>
                      <Switch
                        checked={allowTokenAccess}
                        onCheckedChange={setAllowTokenAccess}
                      />
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={handleCreateConnector}
                      disabled={
                        !name.trim() || createConnectorMutation.isPending
                      }
                    >
                      {createConnectorMutation.isPending
                        ? "Creating..."
                        : "Create"}
                    </Button>
                  </DialogFooter>
                </>
              ) : (
                <>
                  <DialogHeader>
                    <DialogTitle>
                      Create Agent to Agent MCP Connector
                    </DialogTitle>
                    <DialogDescription>
                      <button
                        onClick={() => setCreateStep("type_select")}
                        className="text-primary hover:underline text-sm"
                      >
                        &larr; Back
                      </button>
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4 max-h-[65vh] overflow-y-auto pr-1">
                    <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
                      Share this connector with the users below. Their agents
                      can then connect to this one via{" "}
                      <span className="font-medium">Connect MCP Provider</span>.
                      A direct token is minted automatically when a consumer
                      connects. Manage the token and allowed users later from
                      the connector's edit dialog.
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="a2a-name">Name</Label>
                      <Input
                        id="a2a-name"
                        placeholder="My Agent to Agent Connector"
                        value={a2aName}
                        onChange={(e) => setA2aName(e.target.value)}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Mode</Label>
                      <Select value={a2aMode} onValueChange={setA2aMode}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="conversation">
                            Conversation
                          </SelectItem>
                          <SelectItem value="building">Building</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Conversation mode for chat interactions, Building mode
                        for development tasks.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <UserAllowlistPicker
                        enabled={
                          createDialogOpen &&
                          createStep === "form" &&
                          createType === "agent2agent"
                        }
                        selected={a2aAllowedUsers}
                        onAdd={(u) =>
                          setA2aAllowedUsers((prev) =>
                            prev.some((s) => s.userId === u.id)
                              ? prev
                              : [
                                  ...prev,
                                  {
                                    id: u.id,
                                    userId: u.id,
                                    fallbackLabel: u.full_name || u.email,
                                  },
                                ],
                          )
                        }
                        onRemove={(item) =>
                          setA2aAllowedUsers((prev) =>
                            prev.filter((s) => s.userId !== item.userId),
                          )
                        }
                        label={
                          <Label className="flex items-center gap-2">
                            <Users className="h-4 w-4" />
                            Allowed Users (who may consume)
                          </Label>
                        }
                        searchPlaceholder="Search users..."
                        emptyHint="Leave empty for owner-only access."
                      />
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={handleCreateAgent2Agent}
                      disabled={
                        !a2aName.trim() || createConnectorMutation.isPending
                      }
                    >
                      {createConnectorMutation.isPending
                        ? "Creating..."
                        : "Create"}
                    </Button>
                  </DialogFooter>
                </>
              )}
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>

      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : (
          <div className="space-y-4">
            {/* Empty state. Worth having: this card used to always show at
                least the agent's auto-managed App MCP route, so a blank body
                was unreachable. App MCP exposure is automatic now and has no
                row here, which leaves an agent with no connectors rendering
                nothing at all — and no hint that it is already reachable. */}
            {connectors.length === 0 && a2aConnectors.length === 0 && (
              <div className="rounded-md border border-dashed p-4 space-y-2 text-sm">
                <p className="font-medium">No dedicated connectors</p>
                <p className="text-xs text-muted-foreground">
                  A direct connector gives this one agent its own MCP endpoint.
                  You do not need one to reach it from your own MCP client — the
                  App MCP Server already routes to every agent you own that has
                  a Trigger Prompt.
                </p>
              </div>
            )}

            {/* ---- Direct Connectors ---- */}
            {connectors.length > 0 && (
              <ListRowGroup>
                {connectors.map((connector) => (
                  <McpConnectorRow
                    key={connector.id}
                    connector={connector}
                    serverUrl={getMcpServerUrl(connector.id)}
                    copied={copiedId === connector.id}
                    onCopyUrl={(url) => handleCopyUrl(url, connector.id)}
                    onEdit={() => handleEditConnectorOpen(connector)}
                    onToggleActive={(isActive) =>
                      toggleConnectorMutation.mutate({
                        connectorId: connector.id,
                        isActive,
                      })
                    }
                    onDelete={() => setDeleteTarget(connector)}
                    isPending={
                      toggleConnectorMutation.isPending &&
                      toggleConnectorMutation.variables?.connectorId ===
                        connector.id
                    }
                  />
                ))}
              </ListRowGroup>
            )}

            {/* Separator before the agent-to-agent sub-section */}
            {connectors.length > 0 && a2aConnectors.length > 0 && <Separator />}

            {/* ---- Agent to Agent MCP Connectors ---- */}
            {a2aConnectors.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                  Agent to Agent MCP Connector
                </p>
                <ListRowGroup>
                  {a2aConnectors.map((connector) => (
                    <McpConnectorRow
                      key={connector.id}
                      connector={connector}
                      isAgentToAgent
                      serverUrl={getMcpServerUrl(connector.id)}
                      copied={copiedId === connector.id}
                      onCopyUrl={(url) => handleCopyUrl(url, connector.id)}
                      onEdit={() => handleEditConnectorOpen(connector)}
                      onToggleActive={(isActive) =>
                        toggleConnectorMutation.mutate({
                          connectorId: connector.id,
                          isActive,
                        })
                      }
                      onDelete={() => setDeleteTarget(connector)}
                      isPending={
                        toggleConnectorMutation.isPending &&
                        toggleConnectorMutation.variables?.connectorId ===
                          connector.id
                      }
                    />
                  ))}
                </ListRowGroup>
              </div>
            )}
          </div>
        )}
      </CardContent>

      {/* One confirm for both lists, driven by the row the menu named: an
          `AlertDialog` nested in a menu item loses its pending state when the
          item unmounts on select. */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(next) => {
          if (!deleteConnectorMutation.isPending && !next) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete connector</AlertDialogTitle>
            <AlertDialogDescription>
              Delete <strong>{deleteTarget?.name}</strong>?{" "}
              {deleteTarget?.is_agent_to_agent
                ? "Every agent consuming this connector is disconnected and its tokens are revoked."
                : "Every MCP client using this connector is disconnected and its tokens are revoked."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteConnectorMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (deleteTarget)
                  deleteConnectorMutation.mutate(deleteTarget.id)
              }}
              disabled={deleteConnectorMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteConnectorMutation.isPending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ---- Edit Direct Connector Dialog ---- */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit MCP Connector</DialogTitle>
            <DialogDescription>
              Update the connector name, mode, allowed users, or token access.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 max-h-[65vh] overflow-y-auto pr-1">
            <div className="space-y-2">
              <Label htmlFor="edit-connector-name">Name</Label>
              <Input
                id="edit-connector-name"
                placeholder="My Connector"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Mode</Label>
              <Select value={editMode} onValueChange={setEditMode}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="conversation">Conversation</SelectItem>
                  <SelectItem value="building">Building</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Conversation mode for chat interactions, Building mode for
                development tasks.
              </p>
            </div>
            <div className="space-y-2">
              <UserAllowlistPicker
                enabled={editDialogOpen}
                selected={editAllowedUsers}
                onAdd={(u) =>
                  setEditAllowedUsers((prev) =>
                    prev.some((s) => s.userId === u.id)
                      ? prev
                      : [
                          ...prev,
                          {
                            id: u.id,
                            userId: u.id,
                            fallbackLabel: u.full_name || u.email,
                          },
                        ],
                  )
                }
                onRemove={(item) =>
                  setEditAllowedUsers((prev) =>
                    prev.filter((s) => s.userId !== item.userId),
                  )
                }
                label={
                  <Label className="flex items-center gap-2">
                    <Users className="h-4 w-4" />
                    Allowed Users (optional)
                  </Label>
                }
                searchPlaceholder="Search users..."
                emptyHint="Leave empty for owner-only access."
              />
              {editingConnector &&
                editingConnector.allowed_emails.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    Legacy allowed emails (fallback):{" "}
                    {editingConnector.allowed_emails.join(", ")}
                  </p>
                )}
            </div>
            {/* Agent2agent connectors expose their agent as a peer MCP server
                for other agents (not external LLM clients): token access is
                auto-enabled and managed by the consumer connect helper, and the
                public MCP URL is not user-facing. Hide both external-only blocks
                so the agent2agent edit form mirrors its create form (name / mode
                / allowed users only). */}
            {editingConnector && !editingConnector.is_agent_to_agent && (
              <>
                <Separator />
                <div className="flex items-center justify-between py-1">
                  <div className="space-y-0.5 pr-4">
                    <Label className="text-sm">Allow token access</Label>
                    <p className="text-xs text-muted-foreground">
                      When off, clients must authorize via OAuth. When on, you
                      can generate a direct access token that a client uses
                      without an account — it connects under your name, for this
                      connector only.
                    </p>
                  </div>
                  <Switch
                    checked={editAllowTokenAccess}
                    onCheckedChange={setEditAllowTokenAccess}
                  />
                </div>
                {editAllowTokenAccess && (
                  <>
                    <Separator />
                    <McpDirectTokensManager
                      agentId={agentId}
                      connectorId={editingConnector.id}
                    />
                  </>
                )}
              </>
            )}
            {editingConnector && !editingConnector.is_agent_to_agent && (
              <div className="space-y-2">
                <Label>MCP Server URL</Label>
                {getMcpServerUrl(editingConnector.id) ? (
                  <div className="flex gap-2">
                    <Input
                      value={getMcpServerUrl(editingConnector.id)!}
                      readOnly
                      className="font-mono text-xs"
                    />
                    <Button
                      variant="outline"
                      size="icon"
                      className="shrink-0"
                      onClick={() =>
                        handleCopyUrl(
                          getMcpServerUrl(editingConnector.id)!,
                          editingConnector.id,
                        )
                      }
                    >
                      {copiedId === editingConnector.id ? (
                        <Check className="h-4 w-4 text-green-500" />
                      ) : (
                        <Copy className="h-4 w-4" />
                      )}
                    </Button>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground italic">
                    MCP_SERVER_BASE_URL not configured on the server.
                  </p>
                )}
                <p className="text-xs text-muted-foreground">
                  Use this URL in Claude Desktop or Cursor to connect.
                </p>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleEditConnectorSave}
              disabled={!editName.trim() || updateConnectorMutation.isPending}
            >
              {updateConnectorMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
