import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Copy,
  Check,
  Plus,
  Trash2,
  Unplug,
  Pencil,
} from "lucide-react"
import { useState } from "react"

import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

const API_BASE = import.meta.env.VITE_API_URL || ""

function getAuthHeaders() {
  const token = localStorage.getItem("access_token")
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

interface McpConnector {
  id: string
  agent_id: string
  owner_id: string
  name: string
  mode: string
  is_active: boolean
  allowed_emails: string[]
  max_clients: number
  mcp_server_url: string | null
  created_at: string
  updated_at: string
}

interface McpConnectorsCardProps {
  agentId: string
}

export function McpConnectorsCard({ agentId }: McpConnectorsCardProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [name, setName] = useState("")
  const [mode, setMode] = useState("conversation")
  const [allowedEmails, setAllowedEmails] = useState("")
  const [copiedId, setCopiedId] = useState<string | null>(null)

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingConnector, setEditingConnector] = useState<McpConnector | null>(null)
  const [editName, setEditName] = useState("")
  const [editMode, setEditMode] = useState("conversation")
  const [editAllowedEmails, setEditAllowedEmails] = useState("")

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data, isLoading } = useQuery<{ data: McpConnector[]; count: number; mcp_server_base_url: string | null }>({
    queryKey: ["mcp-connectors", agentId],
    queryFn: async () => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors`,
        { headers: getAuthHeaders() },
      )
      if (!res.ok) throw new Error("Failed to load connectors")
      return res.json()
    },
  })

  const connectors = data?.data ?? []
  const mcpServerBaseUrl = data?.mcp_server_base_url ?? null

  const getMcpServerUrl = (connectorId: string) =>
    mcpServerBaseUrl ? `${mcpServerBaseUrl}/${connectorId}/mcp` : null

  const createMutation = useMutation({
    mutationFn: async (body: {
      name: string
      mode: string
      allowed_emails: string[]
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
        throw new Error(err.detail || "Failed to create connector")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("MCP connector created")
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
      handleDialogClose(false)
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create connector")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: async (connectorId: string) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors/${connectorId}`,
        { method: "DELETE", headers: getAuthHeaders() },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || "Failed to delete connector")
      }
    },
    onSuccess: () => {
      showSuccessToast("Connector deleted")
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete connector")
    },
  })

  const toggleActiveMutation = useMutation({
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
        throw new Error(err.detail || "Failed to update connector")
      }
      return res.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update connector")
    },
  })

  const updateMutation = useMutation({
    mutationFn: async ({
      connectorId,
      body,
    }: {
      connectorId: string
      body: { name?: string; mode?: string; allowed_emails?: string[] }
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
        throw new Error(err.detail || "Failed to update connector")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("Connector updated")
      setEditDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update connector")
    },
  })

  const handleDialogClose = (open: boolean) => {
    setCreateDialogOpen(open)
    if (!open) {
      setName("")
      setMode("conversation")
      setAllowedEmails("")
    }
  }

  const handleCreate = () => {
    const emails = allowedEmails
      .split(",")
      .map((e) => e.trim())
      .filter(Boolean)
    createMutation.mutate({ name, mode, allowed_emails: emails })
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

  const handleEditOpen = (connector: McpConnector) => {
    setEditingConnector(connector)
    setEditName(connector.name)
    setEditMode(connector.mode)
    setEditAllowedEmails(connector.allowed_emails.join(", "))
    setEditDialogOpen(true)
  }

  const handleEditSave = () => {
    if (!editingConnector) return
    const body: { name?: string; mode?: string; allowed_emails?: string[] } = {}
    if (editName !== editingConnector.name) {
      body.name = editName
    }
    if (editMode !== editingConnector.mode) {
      body.mode = editMode
    }
    const newEmails = editAllowedEmails
      .split(",")
      .map((e) => e.trim())
      .filter(Boolean)
    const oldEmails = editingConnector.allowed_emails
    if (JSON.stringify(newEmails) !== JSON.stringify(oldEmails)) {
      body.allowed_emails = newEmails
    }
    updateMutation.mutate({ connectorId: editingConnector.id, body })
  }

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
              <DialogHeader>
                <DialogTitle>Create MCP Connector</DialogTitle>
                <DialogDescription>
                  Create a new MCP server endpoint for this agent.
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
                  <Label htmlFor="allowed-emails">
                    Allowed Emails (optional)
                  </Label>
                  <Input
                    id="allowed-emails"
                    placeholder="user@example.com, other@example.com"
                    value={allowedEmails}
                    onChange={(e) => setAllowedEmails(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Comma-separated. Leave empty for owner-only access.
                  </p>
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={handleCreate}
                  disabled={!name.trim() || createMutation.isPending}
                >
                  {createMutation.isPending ? "Creating..." : "Create"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : connectors.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No MCP connectors yet. Create one to allow external clients to
            connect.
          </p>
        ) : (
          <div className="space-y-1.5">
            {connectors.map((connector) => (
              <div
                key={connector.id}
                className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                  !connector.is_active ? "opacity-50 bg-muted" : ""
                }`}
              >
                {/* Left: name, mode badge, status badge, emails */}
                <div className="flex items-center gap-2 min-w-0">
                  <span className="font-medium text-sm truncate">
                    {connector.name}
                  </span>
                  <Badge variant="secondary" className="text-xs shrink-0">
                    {connector.mode}
                  </Badge>
                  {connector.is_active ? (
                    <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                      Active
                    </Badge>
                  ) : (
                    <Badge variant="destructive" className="text-xs shrink-0">
                      Inactive
                    </Badge>
                  )}
                  {connector.allowed_emails.length > 0 && (
                    <span className="text-xs text-muted-foreground shrink-0">
                      {connector.allowed_emails.length} email{connector.allowed_emails.length !== 1 ? "s" : ""}
                    </span>
                  )}
                </div>
                {/* Right: action buttons */}
                <div className="flex items-center gap-0.5 ml-1 shrink-0">
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          disabled={!getMcpServerUrl(connector.id)}
                          onClick={() => {
                            const url = getMcpServerUrl(connector.id)
                            if (url) handleCopyUrl(url, connector.id)
                          }}
                        >
                          {copiedId === connector.id ? (
                            <Check className="h-3.5 w-3.5 text-green-500" />
                          ) : (
                            <Copy className="h-3.5 w-3.5" />
                          )}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        {getMcpServerUrl(connector.id)
                          ? "Copy MCP server URL"
                          : "MCP_SERVER_BASE_URL not configured"}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <div className="h-4 w-px bg-border mx-1" />
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() => handleEditOpen(connector)}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Edit connector
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() =>
                            toggleActiveMutation.mutate({
                              connectorId: connector.id,
                              isActive: !connector.is_active,
                            })
                          }
                        >
                          <Unplug
                            className={`h-3.5 w-3.5 ${connector.is_active ? "text-emerald-500" : "text-muted-foreground"}`}
                          />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        {connector.is_active ? "Deactivate" : "Activate"}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6 text-destructive hover:text-destructive"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>
                          Delete Connector
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                          This will disconnect all MCP clients using this
                          connector and revoke their tokens.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          onClick={() =>
                            deleteMutation.mutate(connector.id)
                          }
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                        >
                          Delete
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit MCP Connector</DialogTitle>
            <DialogDescription>
              Update the connector name, mode, or allowed emails.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
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
              <Label htmlFor="edit-allowed-emails">
                Allowed Emails (optional)
              </Label>
              <Input
                id="edit-allowed-emails"
                placeholder="user@example.com, other@example.com"
                value={editAllowedEmails}
                onChange={(e) => setEditAllowedEmails(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Comma-separated. Leave empty for owner-only access.
              </p>
            </div>

            {editingConnector && (
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
              onClick={handleEditSave}
              disabled={!editName.trim() || updateMutation.isPending}
            >
              {updateMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}