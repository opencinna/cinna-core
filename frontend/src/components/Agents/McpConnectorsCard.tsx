import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Copy,
  Check,
  Plus,
  Trash2,
  Unplug,
  Pencil,
  Network,
  Users,
  X,
  MessageCircle,
  Wrench,
} from "lucide-react"
import { useState } from "react"

import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
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
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
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

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

interface AppMcpRouteAssignment {
  id: string
  user_id: string
  user_email?: string | null
  user_full_name?: string | null
  is_enabled: boolean
  route_id: string
  created_at: string
}

interface AppMcpRoute {
  id: string
  name: string
  agent_id: string
  agent_name: string
  session_mode: string
  trigger_prompt: string
  message_patterns: string | null
  prompt_examples: string | null
  is_active: boolean
  auto_enable_for_users: boolean
  agent_owner_name: string
  agent_owner_email: string
  created_by: string
  assignments: AppMcpRouteAssignment[]
}

interface IdentityAgentBinding {
  id: string
  agent_id: string
  agent_name: string
  trigger_prompt: string
  message_patterns: string | null
  prompt_examples: string | null
  session_mode: string
  is_active: boolean
  assignments: Array<{
    id: string
    target_user_id: string
    target_user_name: string
    target_user_email: string
    is_active: boolean
  }>
}

type CreateStep = "type_select" | "form"
type CreateType = "direct" | "app_mcp" | "identity_mcp"

interface McpConnectorsCardProps {
  agentId: string
  agentName: string
}

// ---------------------------------------------------------------------------
// McpConnectorsCard
// ---------------------------------------------------------------------------

export function McpConnectorsCard({ agentId, agentName }: McpConnectorsCardProps) {
  const { user: currentUser } = useAuth()
  const isAdmin = currentUser?.is_superuser ?? false

  // ---- Create dialog state ----
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [createStep, setCreateStep] = useState<CreateStep>("type_select")
  const [createType, setCreateType] = useState<CreateType>("direct")

  // Direct connector form
  const [name, setName] = useState("")
  const [mode, setMode] = useState("conversation")
  const [allowedEmails, setAllowedEmails] = useState("")
  const [copiedId, setCopiedId] = useState<string | null>(null)

  // Edit connector state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingConnector, setEditingConnector] = useState<McpConnector | null>(null)
  const [editName, setEditName] = useState("")
  const [editMode, setEditMode] = useState("conversation")
  const [editAllowedEmails, setEditAllowedEmails] = useState("")

  // App MCP route form
  const [appMcpName, setAppMcpName] = useState("")
  const [appMcpSessionMode, setAppMcpSessionMode] = useState("conversation")
  const [appMcpTriggerPrompt, setAppMcpTriggerPrompt] = useState("")
  const [appMcpMessagePatterns, setAppMcpMessagePatterns] = useState("")
  const [appMcpActivateForMyself, setAppMcpActivateForMyself] = useState(true)
  const [appMcpAutoEnable, setAppMcpAutoEnable] = useState(false)
  const [appMcpAssignedUsers, setAppMcpAssignedUsers] = useState<
    UserAllowlistSelectedItem[]
  >([])

  // Edit App MCP route state
  const [editRouteDialogOpen, setEditRouteDialogOpen] = useState(false)
  const [editingRoute, setEditingRoute] = useState<AppMcpRoute | null>(null)
  const [editRouteName, setEditRouteName] = useState("")
  const [editRouteSessionMode, setEditRouteSessionMode] = useState("conversation")
  const [editRouteTriggerPrompt, setEditRouteTriggerPrompt] = useState("")
  const [editRouteMessagePatterns, setEditRouteMessagePatterns] = useState("")
  const [editRouteAutoEnable, setEditRouteAutoEnable] = useState(false)

  // Identity MCP form state
  const [identitySessionMode, setIdentitySessionMode] = useState("conversation")
  const [identityTriggerPrompt, setIdentityTriggerPrompt] = useState("")
  const [identityMessagePatterns, setIdentityMessagePatterns] = useState("")
  const [appMcpPromptExamples, setAppMcpPromptExamples] = useState("")
  const [editRoutePromptExamples, setEditRoutePromptExamples] = useState("")
  const [identityPromptExamples, setIdentityPromptExamples] = useState("")
  const [identityAssignedUsers, setIdentityAssignedUsers] = useState<
    UserAllowlistSelectedItem[]
  >([])

  // Edit Identity Binding state
  const [editIdentityDialogOpen, setEditIdentityDialogOpen] = useState(false)
  const [editIdentityTriggerPrompt, setEditIdentityTriggerPrompt] = useState("")
  const [editIdentityMessagePatterns, setEditIdentityMessagePatterns] = useState("")
  const [editIdentityPromptExamples, setEditIdentityPromptExamples] = useState("")
  const [editIdentitySessionMode, setEditIdentitySessionMode] = useState("conversation")

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
      const res = await fetch(`${API_BASE}/api/v1/agents/${agentId}/mcp-connectors`, {
        headers: getAuthHeaders(),
      })
      if (!res.ok) throw new Error("Failed to load connectors")
      return res.json()
    },
  })

  const { data: appMcpRoutes = [], isLoading: isLoadingRoutes } = useQuery<AppMcpRoute[]>({
    queryKey: ["app-mcp-routes", agentId],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/`, {
        headers: getAuthHeaders(),
      })
      if (!res.ok) throw new Error("Failed to load App MCP routes")
      return res.json()
    },
  })

  // Identity bindings — check if this agent is already in the user's identity
  const { data: identityBindings = [] } = useQuery<IdentityAgentBinding[]>({
    queryKey: ["identity-bindings"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/`, {
        headers: getAuthHeaders(),
      })
      if (!res.ok) throw new Error("Failed to load identity bindings")
      return res.json()
    },
    staleTime: 30000,
  })

  const existingIdentityBinding = identityBindings.find((b) => b.agent_id === agentId) ?? null

  // Use live query data for assignments so add/remove updates immediately
  const editRouteLive = editingRoute ? appMcpRoutes.find((r) => r.id === editingRoute.id) ?? editingRoute : null
  const editRouteAssignments = editRouteLive?.assignments ?? []

  // Edit Identity Binding: live assignments (carry target_user_name/email)
  const editIdentityAssignments = existingIdentityBinding?.assignments ?? []

  const connectors = connectorData?.data ?? []
  const mcpServerBaseUrl = connectorData?.mcp_server_base_url ?? null

  const getMcpServerUrl = (connectorId: string) =>
    mcpServerBaseUrl ? `${mcpServerBaseUrl}/${connectorId}/mcp` : null

  // ---- Mutations: Direct Connectors ----

  const createConnectorMutation = useMutation({
    mutationFn: async (body: { name: string; mode: string; allowed_emails: string[] }) => {
      const res = await fetch(`${API_BASE}/api/v1/agents/${agentId}/mcp-connectors`, {
        method: "POST",
        headers: getAuthHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to create connector")
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
        { method: "DELETE", headers: getAuthHeaders() }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to delete connector")
      }
    },
    onSuccess: () => {
      showSuccessToast("Connector deleted")
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const toggleConnectorMutation = useMutation({
    mutationFn: async ({ connectorId, isActive }: { connectorId: string; isActive: boolean }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/mcp-connectors/${connectorId}`,
        {
          method: "PUT",
          headers: getAuthHeaders(),
          body: JSON.stringify({ is_active: isActive }),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update connector")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mcp-connectors", agentId] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const updateConnectorMutation = useMutation({
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
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update connector")
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

  // ---- Mutations: App MCP Routes ----

  const createAppMcpRouteMutation = useMutation({
    mutationFn: async (body: {
      name: string
      agent_id: string
      session_mode: string
      trigger_prompt: string
      message_patterns: string | null
      prompt_examples: string | null
      auto_enable_for_users: boolean
      assigned_user_ids: string[]
      activate_for_myself: boolean
    }) => {
      const res = await fetch(`${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/`, {
        method: "POST",
        headers: getAuthHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to create App MCP route")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("App MCP route created")
      queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] })
      handleDialogClose(false)
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const deleteAppMcpRouteMutation = useMutation({
    mutationFn: async (routeId: string) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/${routeId}`,
        { method: "DELETE", headers: getAuthHeaders() }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to delete App MCP route")
      }
    },
    onSuccess: () => {
      showSuccessToast("App MCP route deleted")
      queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const toggleAppMcpRouteMutation = useMutation({
    mutationFn: async ({ routeId, isActive }: { routeId: string; isActive: boolean }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/${routeId}`,
        {
          method: "PUT",
          headers: getAuthHeaders(),
          body: JSON.stringify({ is_active: isActive }),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update App MCP route")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const updateAppMcpRouteMutation = useMutation({
    mutationFn: async ({
      routeId,
      body,
    }: {
      routeId: string
      body: {
        name?: string
        session_mode?: string
        trigger_prompt?: string
        message_patterns?: string | null
        prompt_examples?: string | null
        auto_enable_for_users?: boolean
      }
    }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/${routeId}`,
        {
          method: "PUT",
          headers: getAuthHeaders(),
          body: JSON.stringify(body),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update App MCP route")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("App MCP route updated")
      setEditRouteDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const addRouteAssignmentMutation = useMutation({
    mutationFn: async ({ routeId, userIds }: { routeId: string; userIds: string[] }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/${routeId}/assignments`,
        {
          method: "POST",
          headers: getAuthHeaders(),
          body: JSON.stringify(userIds),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to assign user")
      }
      return res.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const removeRouteAssignmentMutation = useMutation({
    mutationFn: async ({ routeId, userId }: { routeId: string; userId: string }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/agents/${agentId}/app-mcp-routes/${routeId}/assignments/${userId}`,
        { method: "DELETE", headers: getAuthHeaders() }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to remove assignment")
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["app-mcp-routes", agentId] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const createIdentityBindingMutation = useMutation({
    mutationFn: async (body: {
      agent_id: string
      trigger_prompt: string
      message_patterns: string | null
      prompt_examples: string | null
      session_mode: string
      assigned_user_ids: string[]
    }) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/`, {
        method: "POST",
        headers: getAuthHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to add agent to identity")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("Agent added to identity")
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
      handleDialogClose(false)
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const removeIdentityBindingMutation = useMutation({
    mutationFn: async (bindingId: string) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "DELETE",
        headers: getAuthHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to remove from identity")
      }
    },
    onSuccess: () => {
      showSuccessToast("Agent removed from identity")
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
      handleDialogClose(false)
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const removeIdentityAssignmentMutation = useMutation({
    mutationFn: async ({ bindingId, userId }: { bindingId: string; userId: string }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/identity/bindings/${bindingId}/assignments/${userId}`,
        { method: "DELETE", headers: getAuthHeaders() }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to remove assignment")
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const toggleIdentityBindingMutation = useMutation({
    mutationFn: async ({ bindingId, isActive }: { bindingId: string; isActive: boolean }) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "PUT",
        headers: getAuthHeaders(),
        body: JSON.stringify({ is_active: isActive }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to toggle identity binding")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const updateIdentityBindingMutation = useMutation({
    mutationFn: async ({
      bindingId,
      body,
    }: {
      bindingId: string
      body: {
        trigger_prompt?: string
        message_patterns?: string | null
        prompt_examples?: string | null
        session_mode?: string
      }
    }) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "PUT",
        headers: getAuthHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update identity binding")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("Identity binding updated")
      setEditIdentityDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const addIdentityAssignmentMutation = useMutation({
    mutationFn: async ({ bindingId, userIds }: { bindingId: string; userIds: string[] }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/identity/bindings/${bindingId}/assignments`,
        {
          method: "POST",
          headers: getAuthHeaders(),
          body: JSON.stringify(userIds),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to assign user")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
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
      setAllowedEmails("")
      setAppMcpName("")
      setAppMcpSessionMode("conversation")
      setAppMcpTriggerPrompt("")
      setAppMcpMessagePatterns("")
      setAppMcpActivateForMyself(true)
      setAppMcpAutoEnable(false)
      setAppMcpAssignedUsers([])
      setIdentitySessionMode("conversation")
      setIdentityTriggerPrompt("")
      setIdentityMessagePatterns("")
      setAppMcpPromptExamples("")
      setIdentityPromptExamples("")
      setIdentityAssignedUsers([])
    }
  }

  const handleTypeSelect = (type: CreateType) => {
    setCreateType(type)
    setCreateStep("form")
    if (type === "app_mcp" && !appMcpName) {
      setAppMcpName(agentName)
    }
  }

  const handleCreateIdentityBinding = () => {
    createIdentityBindingMutation.mutate({
      agent_id: agentId,
      trigger_prompt: identityTriggerPrompt.trim(),
      message_patterns: identityMessagePatterns.trim() || null,
      prompt_examples: identityPromptExamples.trim() || null,
      session_mode: identitySessionMode,
      assigned_user_ids: identityAssignedUsers.map((s) => s.userId),
    })
  }

  const handleCreateConnector = () => {
    const emails = allowedEmails
      .split(",")
      .map((e) => e.trim())
      .filter(Boolean)
    createConnectorMutation.mutate({ name, mode, allowed_emails: emails })
  }

  const handleCreateAppMcpRoute = () => {
    createAppMcpRouteMutation.mutate({
      name: appMcpName,
      agent_id: agentId,
      session_mode: appMcpSessionMode,
      trigger_prompt: appMcpTriggerPrompt,
      message_patterns: appMcpMessagePatterns || null,
      prompt_examples: appMcpPromptExamples.trim() || null,
      auto_enable_for_users: appMcpAutoEnable,
      assigned_user_ids: appMcpAssignedUsers.map((s) => s.userId),
      activate_for_myself: appMcpActivateForMyself,
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
    setEditAllowedEmails(connector.allowed_emails.join(", "))
    setEditDialogOpen(true)
  }

  const handleEditConnectorSave = () => {
    if (!editingConnector) return
    const body: { name?: string; mode?: string; allowed_emails?: string[] } = {}
    if (editName !== editingConnector.name) body.name = editName
    if (editMode !== editingConnector.mode) body.mode = editMode
    const newEmails = editAllowedEmails
      .split(",")
      .map((e) => e.trim())
      .filter(Boolean)
    if (JSON.stringify(newEmails) !== JSON.stringify(editingConnector.allowed_emails)) {
      body.allowed_emails = newEmails
    }
    updateConnectorMutation.mutate({ connectorId: editingConnector.id, body })
  }

  const handleEditIdentityOpen = (binding: IdentityAgentBinding) => {
    setEditIdentityTriggerPrompt(binding.trigger_prompt)
    setEditIdentityMessagePatterns(binding.message_patterns ?? "")
    setEditIdentityPromptExamples(binding.prompt_examples ?? "")
    setEditIdentitySessionMode(binding.session_mode)
    setEditIdentityDialogOpen(true)
  }

  const handleEditIdentitySave = () => {
    if (!existingIdentityBinding) return
    updateIdentityBindingMutation.mutate({
      bindingId: existingIdentityBinding.id,
      body: {
        trigger_prompt: editIdentityTriggerPrompt.trim(),
        message_patterns: editIdentityMessagePatterns.trim() || null,
        prompt_examples: editIdentityPromptExamples.trim() || null,
        session_mode: editIdentitySessionMode,
      },
    })
  }

  const handleEditRouteOpen = (route: AppMcpRoute) => {
    setEditingRoute(route)
    setEditRouteName(route.name)
    setEditRouteSessionMode(route.session_mode)
    setEditRouteTriggerPrompt(route.trigger_prompt)
    setEditRouteMessagePatterns(route.message_patterns ?? "")
    setEditRoutePromptExamples(route.prompt_examples ?? "")
    setEditRouteAutoEnable(route.auto_enable_for_users)
    setEditRouteDialogOpen(true)
  }

  const handleEditRouteSave = () => {
    if (!editingRoute) return
    updateAppMcpRouteMutation.mutate({
      routeId: editingRoute.id,
      body: {
        name: editRouteName,
        session_mode: editRouteSessionMode,
        trigger_prompt: editRouteTriggerPrompt,
        message_patterns: editRouteMessagePatterns || null,
        prompt_examples: editRoutePromptExamples.trim() || null,
        auto_enable_for_users: editRouteAutoEnable,
      },
    })
  }

  const isLoading = isLoadingConnectors || isLoadingRoutes

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
              Connect external MCP clients (Claude Desktop, Cursor) to this agent
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
                        <span className="font-medium text-sm">Direct MCP Connector</span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Dedicated MCP endpoint for this agent. External clients connect directly to this specific agent.
                      </p>
                    </button>

                    <button
                      onClick={() => handleTypeSelect("app_mcp")}
                      className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                    >
                      <div className="flex items-center gap-2">
                        <Network className="h-5 w-5 text-blue-500" />
                        <span className="font-medium text-sm">App MCP Server Integration</span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Add this agent to the shared App MCP Server. Users connect once and access multiple agents through automatic routing.
                      </p>
                    </button>

                    <button
                      onClick={() => handleTypeSelect("identity_mcp")}
                      className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                    >
                      <div className="flex items-center gap-2">
                        <Users className="h-5 w-5 text-violet-500" />
                        <span className="font-medium text-sm">Identity MCP Server Integration</span>
                        {existingIdentityBinding && (
                          <Badge variant="outline" className="text-xs border-violet-300 text-violet-600">
                            Active
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Expose this agent behind your personal identity. Other users can address you by name and the system routes to this agent automatically.
                      </p>
                    </button>
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
                          <SelectItem value="conversation">Conversation</SelectItem>
                          <SelectItem value="building">Building</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Conversation mode for chat interactions, Building mode for development tasks.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="allowed-emails">Allowed Emails (optional)</Label>
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
                      onClick={handleCreateConnector}
                      disabled={!name.trim() || createConnectorMutation.isPending}
                    >
                      {createConnectorMutation.isPending ? "Creating..." : "Create"}
                    </Button>
                  </DialogFooter>
                </>
              ) : createType === "identity_mcp" ? (
                // Identity MCP Server form
                <>
                  <DialogHeader>
                    <DialogTitle>Add to Identity Server</DialogTitle>
                    <DialogDescription>
                      <button
                        onClick={() => setCreateStep("type_select")}
                        className="text-primary hover:underline text-sm"
                      >
                        &larr; Back
                      </button>
                    </DialogDescription>
                  </DialogHeader>

                  {existingIdentityBinding ? (
                    // Already in identity — show existing binding with management options
                    <div className="space-y-4">
                      <div className="flex items-center gap-2 p-3 bg-violet-50 dark:bg-violet-900/20 rounded-lg border border-violet-200 dark:border-violet-800">
                        <Users className="h-4 w-4 text-violet-500 shrink-0" />
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-violet-700 dark:text-violet-300">
                            Part of Identity Server
                          </p>
                          <p className="text-xs text-muted-foreground truncate">
                            {existingIdentityBinding.trigger_prompt}
                          </p>
                        </div>
                      </div>
                      {existingIdentityBinding.assignments.length > 0 && (
                        <div className="space-y-1.5">
                          <p className="text-xs font-medium text-muted-foreground">Shared with</p>
                          <div className="flex flex-wrap gap-1.5">
                            {existingIdentityBinding.assignments.map((a) => (
                              <span
                                key={a.id}
                                className="flex items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
                              >
                                {a.target_user_name || a.target_user_email}
                                <button
                                  type="button"
                                  onClick={() =>
                                    removeIdentityAssignmentMutation.mutate({
                                      bindingId: existingIdentityBinding.id,
                                      userId: a.target_user_id,
                                    })
                                  }
                                  className="hover:text-destructive transition-colors"
                                  disabled={removeIdentityAssignmentMutation.isPending}
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      <p className="text-xs text-muted-foreground">
                        Manage the full Identity Server configuration in{" "}
                        <a
                          href="/settings#channels"
                          className="text-primary hover:underline"
                          onClick={() => handleDialogClose(false)}
                        >
                          Settings &rarr; Channels
                        </a>
                        .
                      </p>
                      <DialogFooter>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => removeIdentityBindingMutation.mutate(existingIdentityBinding.id)}
                          disabled={removeIdentityBindingMutation.isPending}
                        >
                          {removeIdentityBindingMutation.isPending
                            ? "Removing..."
                            : "Remove from Identity"}
                        </Button>
                      </DialogFooter>
                    </div>
                  ) : (
                    // Not yet in identity — show creation form
                    <>
                      <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
                        <div className="space-y-2">
                          <Label>Session Mode</Label>
                          <Select value={identitySessionMode} onValueChange={setIdentitySessionMode}>
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="conversation">Conversation</SelectItem>
                              <SelectItem value="building">Building</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="space-y-2">
                          <Label htmlFor="identity-trigger">Trigger Prompt</Label>
                          <Textarea
                            id="identity-trigger"
                            placeholder="Describe when to route to this agent (e.g. 'Handle annual report requests and financial analysis')"
                            value={identityTriggerPrompt}
                            onChange={(e) => setIdentityTriggerPrompt(e.target.value)}
                            rows={3}
                          />
                          <p className="text-xs text-muted-foreground">
                            Used when someone addresses you to select this agent over others in your identity.
                          </p>
                        </div>
                        <div className="space-y-2">
                          <Label htmlFor="identity-patterns">Message Patterns (optional)</Label>
                          <Textarea
                            id="identity-patterns"
                            placeholder={"annual report *\nfinancial analysis *"}
                            value={identityMessagePatterns}
                            onChange={(e) => setIdentityMessagePatterns(e.target.value)}
                            rows={2}
                          />
                          <p className="text-xs text-muted-foreground">
                            One glob-style pattern per line. Matched before AI routing.
                          </p>
                        </div>

                        <div className="space-y-2">
                          <Label htmlFor="identity-prompt-examples">Prompt Examples (optional)</Label>
                          <Textarea
                            id="identity-prompt-examples"
                            placeholder={"generate employee report\nprepare quarterly analysis"}
                            value={identityPromptExamples}
                            onChange={(e) => setIdentityPromptExamples(e.target.value)}
                            rows={3}
                            className="font-mono text-sm"
                          />
                          <p className="text-xs text-muted-foreground">
                            Short example prompts. MCP clients will see these prefixed with your name (e.g., 'ask Your Name to generate employee report').
                          </p>
                        </div>

                        <div className="space-y-2">
                          <UserAllowlistPicker
                            enabled={
                              createDialogOpen &&
                              createStep === "form" &&
                              createType === "identity_mcp"
                            }
                            selected={identityAssignedUsers}
                            onAdd={(u) =>
                              setIdentityAssignedUsers((prev) =>
                                prev.some((s) => s.userId === u.id)
                                  ? prev
                                  : [
                                      ...prev,
                                      {
                                        id: u.id,
                                        userId: u.id,
                                        fallbackLabel: u.full_name || u.email,
                                      },
                                    ]
                              )
                            }
                            onRemove={(item) =>
                              setIdentityAssignedUsers((prev) =>
                                prev.filter((s) => s.userId !== item.userId)
                              )
                            }
                            label={
                              <Label className="flex items-center gap-2">
                                <Users className="h-4 w-4" />
                                Share with Users
                              </Label>
                            }
                            searchPlaceholder="Search users..."
                          />
                          <p className="text-xs text-muted-foreground">
                            View and manage all identity settings in{" "}
                            <a
                              href="/settings#channels"
                              className="text-primary hover:underline"
                              onClick={() => handleDialogClose(false)}
                            >
                              Settings &rarr; Channels
                            </a>
                            .
                          </p>
                        </div>
                      </div>
                      <DialogFooter>
                        <Button
                          onClick={handleCreateIdentityBinding}
                          disabled={
                            !identityTriggerPrompt.trim() ||
                            createIdentityBindingMutation.isPending
                          }
                        >
                          {createIdentityBindingMutation.isPending
                            ? "Adding..."
                            : "Add to Identity"}
                        </Button>
                      </DialogFooter>
                    </>
                  )}
                </>
              ) : (
                // App MCP Server form
                <>
                  <DialogHeader>
                    <DialogTitle>Add to App MCP Server</DialogTitle>
                    <DialogDescription>
                      <button
                        onClick={() => setCreateStep("type_select")}
                        className="text-primary hover:underline text-sm"
                      >
                        &larr; Back
                      </button>
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
                    <div className="space-y-2">
                      <Label htmlFor="app-mcp-name">Name</Label>
                      <Input
                        id="app-mcp-name"
                        placeholder="Code Review Assistant"
                        value={appMcpName}
                        onChange={(e) => setAppMcpName(e.target.value)}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Session Mode</Label>
                      <Select value={appMcpSessionMode} onValueChange={setAppMcpSessionMode}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="conversation">Conversation</SelectItem>
                          <SelectItem value="building">Building</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="app-mcp-trigger">Trigger Prompt</Label>
                      <Textarea
                        id="app-mcp-trigger"
                        placeholder="Describe when to route messages to this agent (e.g. 'Handle code review requests and PR analysis')"
                        value={appMcpTriggerPrompt}
                        onChange={(e) => setAppMcpTriggerPrompt(e.target.value)}
                        rows={3}
                      />
                      <p className="text-xs text-muted-foreground">
                        Used by the AI router to match messages to this agent.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="app-mcp-patterns">Message Patterns (optional)</Label>
                      <Textarea
                        id="app-mcp-patterns"
                        placeholder={"review this PR *\ncheck my code *"}
                        value={appMcpMessagePatterns}
                        onChange={(e) => setAppMcpMessagePatterns(e.target.value)}
                        rows={2}
                      />
                      <p className="text-xs text-muted-foreground">
                        One glob-style pattern per line. Pattern matching runs before AI routing.
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="app-mcp-prompt-examples">Prompt Examples (optional)</Label>
                      <Textarea
                        id="app-mcp-prompt-examples"
                        placeholder={"generate employee report\nsummarize last quarter sales"}
                        value={appMcpPromptExamples}
                        onChange={(e) => setAppMcpPromptExamples(e.target.value)}
                        rows={3}
                        className="font-mono text-sm"
                      />
                      <p className="text-xs text-muted-foreground">
                        Short example prompts shown to MCP clients. One per line. These skip the 'ask cinna to...' prefix.
                      </p>
                    </div>

                    <Separator />

                    <div className="flex items-center justify-between py-1">
                      <div className="space-y-0.5">
                        <Label className="text-sm">Activate for Myself</Label>
                        <p className="text-xs text-muted-foreground">
                          Enable this agent in your own App MCP Server.
                        </p>
                      </div>
                      <Switch
                        checked={appMcpActivateForMyself}
                        onCheckedChange={setAppMcpActivateForMyself}
                      />
                    </div>

                    <div className="space-y-2">
                      <UserAllowlistPicker
                        enabled={
                          createDialogOpen &&
                          createStep === "form" &&
                          createType === "app_mcp"
                        }
                        selected={appMcpAssignedUsers}
                        onAdd={(u) =>
                          setAppMcpAssignedUsers((prev) =>
                            prev.some((s) => s.userId === u.id)
                              ? prev
                              : [
                                  ...prev,
                                  {
                                    id: u.id,
                                    userId: u.id,
                                    fallbackLabel: u.full_name || u.email,
                                  },
                                ]
                          )
                        }
                        onRemove={(item) =>
                          setAppMcpAssignedUsers((prev) =>
                            prev.filter((s) => s.userId !== item.userId)
                          )
                        }
                        label={
                          <Label className="flex items-center gap-2">
                            <Users className="h-4 w-4" />
                            Share with Users
                          </Label>
                        }
                        searchPlaceholder="Search users..."
                      />
                    </div>

                    <div className="flex items-center justify-between py-1">
                      <div className="space-y-0.5">
                        <Label className="text-sm">Make Active for Users</Label>
                        <p className="text-xs text-muted-foreground">
                          When enabled, assigned users can use this agent immediately without manual activation.
                        </p>
                      </div>
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span>
                              <Switch
                                checked={appMcpAutoEnable}
                                onCheckedChange={isAdmin ? setAppMcpAutoEnable : undefined}
                                disabled={!isAdmin}
                              />
                            </span>
                          </TooltipTrigger>
                          {!isAdmin && (
                            <TooltipContent side="left" className="text-xs max-w-48">
                              Only administrators can activate routes for users immediately.
                            </TooltipContent>
                          )}
                        </Tooltip>
                      </TooltipProvider>
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={handleCreateAppMcpRoute}
                      disabled={
                        !appMcpName.trim() ||
                        !appMcpTriggerPrompt.trim() ||
                        createAppMcpRouteMutation.isPending
                      }
                    >
                      {createAppMcpRouteMutation.isPending ? "Saving..." : "Save"}
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
            {/* ---- Direct Connectors ---- */}
            {connectors.length > 0 && (
              <div className="space-y-1.5">
                {connectors.map((connector) => (
                  <div
                    key={connector.id}
                    className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                      !connector.is_active ? "opacity-50 bg-muted" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-medium text-sm truncate">{connector.name}</span>
                      {connector.mode === "building" ? (
                        <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                      ) : (
                        <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                      )}
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
                          {connector.allowed_emails.length} email
                          {connector.allowed_emails.length !== 1 ? "s" : ""}
                        </span>
                      )}
                    </div>
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
                              onClick={() => handleEditConnectorOpen(connector)}
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
                                toggleConnectorMutation.mutate({
                                  connectorId: connector.id,
                                  isActive: !connector.is_active,
                                })
                              }
                            >
                              <Unplug
                                className={`h-3.5 w-3.5 ${
                                  connector.is_active ? "text-emerald-500" : "text-muted-foreground"
                                }`}
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
                            <AlertDialogTitle>Delete Connector</AlertDialogTitle>
                            <AlertDialogDescription>
                              This will disconnect all MCP clients using this connector and revoke their tokens.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                              onClick={() => deleteConnectorMutation.mutate(connector.id)}
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

            {/* Separator when both sections have items */}
            {connectors.length > 0 && appMcpRoutes.length > 0 && <Separator />}

            {/* ---- App MCP Routes ---- */}
            {appMcpRoutes.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                  App MCP Server
                </p>
                {appMcpRoutes.map((route) => (
                  <div
                    key={route.id}
                    className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                      !route.is_active ? "opacity-50 bg-muted" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-medium text-sm truncate">{route.name}</span>
                      {route.session_mode === "building" ? (
                        <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                      ) : (
                        <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                      )}
                      <Badge
                        variant="outline"
                        className="text-xs shrink-0 border-blue-300 text-blue-600"
                      >
                        App MCP
                      </Badge>
                      {route.assignments.length > 0 && (
                        <span className="text-xs text-muted-foreground shrink-0">
                          {route.assignments.length} user
                          {route.assignments.length !== 1 ? "s" : ""}
                        </span>
                      )}
                      {route.is_active ? (
                        <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                          Active
                        </Badge>
                      ) : (
                        <Badge variant="destructive" className="text-xs shrink-0">
                          Inactive
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-0.5 ml-1 shrink-0">
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-6 w-6"
                              onClick={() => handleEditRouteOpen(route)}
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Edit route
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
                                toggleAppMcpRouteMutation.mutate({
                                  routeId: route.id,
                                  isActive: !route.is_active,
                                })
                              }
                            >
                              <Network
                                className={`h-3.5 w-3.5 ${
                                  route.is_active ? "text-blue-500" : "text-muted-foreground"
                                }`}
                              />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            {route.is_active ? "Deactivate" : "Activate"}
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
                            <AlertDialogTitle>Delete App MCP Route</AlertDialogTitle>
                            <AlertDialogDescription>
                              This will remove the route and all user assignments. Existing sessions will not be affected.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                              onClick={() => deleteAppMcpRouteMutation.mutate(route.id)}
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

            {/* Empty state */}
            {connectors.length === 0 && appMcpRoutes.length === 0 && !existingIdentityBinding && (
              <p className="text-sm text-muted-foreground">
                No MCP integrations yet. Create one to allow external clients to connect.
              </p>
            )}

            {/* Separator when identity section follows connectors/routes */}
            {existingIdentityBinding &&
              (connectors.length > 0 || appMcpRoutes.length > 0) && <Separator />}

            {/* Identity Server binding */}
            {existingIdentityBinding && (
              <div className="space-y-1.5">
                <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                  Identity Server
                </p>
                <div
                  className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                    !existingIdentityBinding.is_active ? "opacity-50 bg-muted" : ""
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium text-sm truncate">
                      {existingIdentityBinding.agent_name}
                    </span>
                    {existingIdentityBinding.session_mode === "building" ? (
                      <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                    ) : (
                      <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                    )}
                    <Badge
                      variant="outline"
                      className="text-xs shrink-0 border-violet-300 text-violet-600"
                    >
                      Identity
                    </Badge>
                    {existingIdentityBinding.assignments.length > 0 && (
                      <span className="text-xs text-muted-foreground shrink-0">
                        {existingIdentityBinding.assignments.length} user
                        {existingIdentityBinding.assignments.length !== 1 ? "s" : ""}
                      </span>
                    )}
                    {existingIdentityBinding.is_active ? (
                      <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                        Active
                      </Badge>
                    ) : (
                      <Badge variant="destructive" className="text-xs shrink-0">
                        Inactive
                      </Badge>
                    )}
                  </div>
                  <div className="flex items-center gap-0.5 ml-1 shrink-0">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6"
                            onClick={() => handleEditIdentityOpen(existingIdentityBinding)}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          Edit identity binding
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
                              toggleIdentityBindingMutation.mutate({
                                bindingId: existingIdentityBinding.id,
                                isActive: !existingIdentityBinding.is_active,
                              })
                            }
                          >
                            <Users
                              className={`h-3.5 w-3.5 ${
                                existingIdentityBinding.is_active
                                  ? "text-violet-500"
                                  : "text-muted-foreground"
                              }`}
                            />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          {existingIdentityBinding.is_active ? "Deactivate" : "Activate"}
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
                          <AlertDialogTitle>Remove from Identity</AlertDialogTitle>
                          <AlertDialogDescription>
                            This removes {existingIdentityBinding.agent_name} from your identity and
                            revokes access for all assigned users. Existing identity sessions are
                            not affected but cannot receive new messages.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction
                            onClick={() =>
                              removeIdentityBindingMutation.mutate(existingIdentityBinding.id)
                            }
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          >
                            Remove
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </CardContent>

      {/* ---- Edit Direct Connector Dialog ---- */}
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
                Conversation mode for chat interactions, Building mode for development tasks.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-allowed-emails">Allowed Emails (optional)</Label>
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
                        handleCopyUrl(getMcpServerUrl(editingConnector.id)!, editingConnector.id)
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

      {/* ---- Edit App MCP Route Dialog ---- */}
      <Dialog open={editRouteDialogOpen} onOpenChange={setEditRouteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit App MCP Route</DialogTitle>
            <DialogDescription>Update the route configuration.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
            <div className="space-y-2">
              <Label>Name</Label>
              <Input
                value={editRouteName}
                onChange={(e) => setEditRouteName(e.target.value)}
                placeholder="Route name"
              />
            </div>
            <div className="space-y-2">
              <Label>Session Mode</Label>
              <Select value={editRouteSessionMode} onValueChange={setEditRouteSessionMode}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="conversation">Conversation</SelectItem>
                  <SelectItem value="building">Building</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Trigger Prompt</Label>
              <Textarea
                value={editRouteTriggerPrompt}
                onChange={(e) => setEditRouteTriggerPrompt(e.target.value)}
                rows={3}
                placeholder="Describe when to route messages to this agent"
              />
              <p className="text-xs text-muted-foreground">
                Used by the AI router to match messages to this agent.
              </p>
            </div>
            <div className="space-y-2">
              <Label>Message Patterns (optional)</Label>
              <Textarea
                value={editRouteMessagePatterns}
                onChange={(e) => setEditRouteMessagePatterns(e.target.value)}
                rows={2}
                placeholder={"review this PR *\ncheck my code *"}
              />
              <p className="text-xs text-muted-foreground">
                One glob-style pattern per line. Pattern matching runs before AI routing.
              </p>
            </div>

            <div className="space-y-2">
              <Label>Prompt Examples (optional)</Label>
              <Textarea
                value={editRoutePromptExamples}
                onChange={(e) => setEditRoutePromptExamples(e.target.value)}
                rows={3}
                placeholder={"generate employee report\nsummarize last quarter sales"}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                Short example prompts shown to MCP clients. One per line. These skip the 'ask cinna to...' prefix.
              </p>
            </div>

            <Separator />

            <UserAllowlistPicker
              enabled={editRouteDialogOpen}
              selected={editRouteAssignments.map((a) => ({
                id: a.id,
                userId: a.user_id,
                fallbackLabel: a.user_full_name || a.user_email || undefined,
              }))}
              onAdd={(u) => {
                if (editingRoute) {
                  addRouteAssignmentMutation.mutate({
                    routeId: editingRoute.id,
                    userIds: [u.id],
                  })
                }
              }}
              onRemove={(item) => {
                if (editingRoute) {
                  removeRouteAssignmentMutation.mutate({
                    routeId: editingRoute.id,
                    userId: item.userId,
                  })
                }
              }}
              isRemoving={removeRouteAssignmentMutation.isPending}
              label={
                <Label className="flex items-center gap-2">
                  <Users className="h-4 w-4" />
                  Shared with Users
                </Label>
              }
            />

            <div className="flex items-center justify-between py-1">
              <div className="space-y-0.5">
                <Label className="text-sm">Make Active for Users</Label>
                <p className="text-xs text-muted-foreground">
                  When enabled, assigned users can use this agent immediately without manual activation.
                </p>
              </div>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>
                      <Switch
                        checked={editRouteAutoEnable}
                        onCheckedChange={isAdmin ? setEditRouteAutoEnable : undefined}
                        disabled={!isAdmin}
                      />
                    </span>
                  </TooltipTrigger>
                  {!isAdmin && (
                    <TooltipContent side="left" className="text-xs max-w-48">
                      Only administrators can activate routes for users immediately.
                    </TooltipContent>
                  )}
                </Tooltip>
              </TooltipProvider>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditRouteDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleEditRouteSave}
              disabled={
                !editRouteName.trim() ||
                !editRouteTriggerPrompt.trim() ||
                updateAppMcpRouteMutation.isPending
              }
            >
              {updateAppMcpRouteMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---- Edit Identity Binding Dialog ---- */}
      <Dialog open={editIdentityDialogOpen} onOpenChange={setEditIdentityDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Identity Binding</DialogTitle>
            <DialogDescription>
              Update the routing configuration for{" "}
              <strong>{existingIdentityBinding?.agent_name}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
            <div className="space-y-2">
              <Label>Session Mode</Label>
              <Select value={editIdentitySessionMode} onValueChange={setEditIdentitySessionMode}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="conversation">Conversation</SelectItem>
                  <SelectItem value="building">Building</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Trigger Prompt</Label>
              <Textarea
                value={editIdentityTriggerPrompt}
                onChange={(e) => setEditIdentityTriggerPrompt(e.target.value)}
                rows={3}
                placeholder="Describe when to route to this agent"
              />
              <p className="text-xs text-muted-foreground">
                Used when someone addresses you to select this agent over others in your identity.
              </p>
            </div>
            <div className="space-y-2">
              <Label>Message Patterns (optional)</Label>
              <Textarea
                value={editIdentityMessagePatterns}
                onChange={(e) => setEditIdentityMessagePatterns(e.target.value)}
                rows={2}
                placeholder={"annual report *\nfinancial analysis *"}
              />
              <p className="text-xs text-muted-foreground">
                One glob-style pattern per line. Matched before AI routing.
              </p>
            </div>
            <div className="space-y-2">
              <Label>Prompt Examples (optional)</Label>
              <Textarea
                value={editIdentityPromptExamples}
                onChange={(e) => setEditIdentityPromptExamples(e.target.value)}
                rows={3}
                placeholder={"generate employee report\nprepare quarterly analysis"}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                Short example prompts. MCP clients will see these prefixed with your name (e.g.,
                'ask Your Name to generate employee report').
              </p>
            </div>

            <Separator />

            <UserAllowlistPicker
              enabled={editIdentityDialogOpen}
              selected={editIdentityAssignments.map((a) => ({
                id: a.id,
                userId: a.target_user_id,
                fallbackLabel: a.target_user_name || a.target_user_email || undefined,
              }))}
              onAdd={(u) => {
                if (existingIdentityBinding) {
                  addIdentityAssignmentMutation.mutate({
                    bindingId: existingIdentityBinding.id,
                    userIds: [u.id],
                  })
                }
              }}
              onRemove={(item) => {
                if (existingIdentityBinding) {
                  removeIdentityAssignmentMutation.mutate({
                    bindingId: existingIdentityBinding.id,
                    userId: item.userId,
                  })
                }
              }}
              isRemoving={removeIdentityAssignmentMutation.isPending}
              label={
                <Label className="flex items-center gap-2">
                  <Users className="h-4 w-4" />
                  Shared with Users
                </Label>
              }
              searchPlaceholder="Search users to add..."
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditIdentityDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleEditIdentitySave}
              disabled={
                !editIdentityTriggerPrompt.trim() || updateIdentityBindingMutation.isPending
              }
            >
              {updateIdentityBindingMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
