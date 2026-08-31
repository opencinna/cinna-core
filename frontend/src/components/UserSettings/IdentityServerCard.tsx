/**
 * IdentityServerCard — Settings > Channels tab (owner view)
 *
 * Manages the current user's identity: which agents are exposed behind their
 * identity and which users can reach each agent via identity routing.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  UserCircle,
  Pencil,
  Trash2,
  X,
  MessageCircle,
  Wrench,
  Users,
  ChevronDown,
  ChevronUp,
  Plus,
  Bot,
} from "lucide-react"
import { useMemo, useState } from "react"

import { AgentsService, IdentityService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { cn } from "@/lib/utils"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { UserAllowlistPicker } from "@/components/Common/UserAllowlistPicker"
import type { UserAllowlistSelectedItem } from "@/components/Common/UserAllowlistPicker"
import { AgentSelectorDialog } from "@/components/Common/AgentSelectorDialog"
import type { AgentOption } from "@/components/Common/AgentSelectorDialog"

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

interface IdentityBindingAssignment {
  id: string
  binding_id: string
  target_user_id: string
  target_user_name: string
  target_user_email: string
  is_active: boolean
  is_enabled: boolean
  created_at: string
}

interface IdentityAgentBinding {
  id: string
  agent_id: string
  agent_name: string
  trigger_prompt: string
  prompt_examples: string | null
  session_mode: string
  is_active: boolean
  created_at: string
  updated_at: string
  assignments: IdentityBindingAssignment[]
}

// ---------------------------------------------------------------------------
// IdentityServerCard
// ---------------------------------------------------------------------------

export function IdentityServerCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Expanded state per binding (show/hide user assignments)
  const [expandedBindings, setExpandedBindings] = useState<Set<string>>(new Set())

  // Edit binding dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingBinding, setEditingBinding] = useState<IdentityAgentBinding | null>(null)
  const [editTriggerPrompt, setEditTriggerPrompt] = useState("")
  const [editSessionMode, setEditSessionMode] = useState("conversation")
  const [editPromptExamples, setEditPromptExamples] = useState("")

  // Add binding dialog state. Mirrors the edit dialog's fields so the same
  // binding reads the same way whether it is being created or changed. The
  // assignment list is local here (the binding does not exist yet) and is sent
  // with the create call as `assigned_user_ids`.
  const [addDialogOpen, setAddDialogOpen] = useState(false)
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)
  const [addAgentId, setAddAgentId] = useState("")
  const [addTriggerPrompt, setAddTriggerPrompt] = useState("")
  const [addSessionMode, setAddSessionMode] = useState("conversation")
  const [addPromptExamples, setAddPromptExamples] = useState("")
  const [addAssignedUsers, setAddAssignedUsers] = useState<
    UserAllowlistSelectedItem[]
  >([])

  // ---------------------------------------------------------------------------
  // Queries
  // ---------------------------------------------------------------------------

  const { data: bindings = [], isLoading } = useQuery<IdentityAgentBinding[]>({
    queryKey: ["identity-bindings"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/`, {
        headers: getAuthHeaders(),
      })
      if (!res.ok) throw new Error("Failed to load identity bindings")
      return res.json()
    },
  })

  // Agents to choose from when adding a binding. Owner-scoped server-side —
  // `GET /agents/` returns only the caller's own agents, which is the set the
  // identity binding endpoint will accept. Same query key and page size as the
  // other agent pickers so the cache is shared rather than duplicated; only
  // fetched once the add dialog is open. The list is passed to
  // `AgentSelectorDialog` (instead of letting it fetch) because the trigger
  // button needs the selected agent's name and colour.
  const {
    data: agentsData,
    isLoading: isAgentsLoading,
    isError: isAgentsError,
  } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 200 }),
    enabled: addDialogOpen,
  })

  const agentOptions: AgentOption[] = useMemo(
    () =>
      (agentsData?.data ?? []).map((a) => ({
        id: a.id,
        name: a.name,
        colorPreset: a.ui_color_preset,
      })),
    [agentsData],
  )

  const selectedAgent = agentOptions.find((a) => a.id === addAgentId) ?? null
  const selectedAgentPreset = selectedAgent
    ? getColorPreset(selectedAgent.colorPreset)
    : null

  // Edit dialog: live binding data for real-time assignment updates
  const editBindingLive = editingBinding
    ? bindings.find((b) => b.id === editingBinding.id) ?? editingBinding
    : null
  const editAssignments = editBindingLive?.assignments ?? []

  // ---------------------------------------------------------------------------
  // Mutations
  // ---------------------------------------------------------------------------

  // Uses the generated client rather than the raw `fetch` the sibling
  // mutations here still use: the project convention is the generated client,
  // and the create payload is the one call in this card whose shape must stay
  // in step with the backend schema (`IdentityAgentBindingCreate`) — a hand-
  // written body would silently drift the moment a field is added or dropped.
  const createBindingMutation = useMutation({
    mutationFn: ({
      agentId,
      triggerPrompt,
      promptExamples,
      sessionMode,
      assignedUserIds,
    }: {
      agentId: string
      triggerPrompt: string
      promptExamples: string | null
      sessionMode: string
      assignedUserIds: string[]
    }) =>
      IdentityService.createIdentityBinding({
        requestBody: {
          agent_id: agentId,
          trigger_prompt: triggerPrompt,
          prompt_examples: promptExamples,
          session_mode: sessionMode,
          assigned_user_ids: assignedUserIds,
        },
      }),
    onSuccess: () => {
      showSuccessToast("Agent added to identity")
      setAddDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to add agent to identity")),
  })

  const updateBindingMutation = useMutation({
    mutationFn: async ({
      bindingId,
      body,
    }: {
      bindingId: string
      body: {
        trigger_prompt?: string
        prompt_examples?: string | null
        session_mode?: string
        is_active?: boolean
      }
    }) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "PUT",
        headers: getAuthHeaders(),
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to update binding")
      }
      return res.json()
    },
    onSuccess: () => {
      showSuccessToast("Binding updated")
      setEditDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const deleteBindingMutation = useMutation({
    mutationFn: async (bindingId: string) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "DELETE",
        headers: getAuthHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to delete binding")
      }
    },
    onSuccess: () => {
      showSuccessToast("Agent removed from identity")
      queryClient.invalidateQueries({ queryKey: ["identity-bindings"] })
    },
    onError: (error: Error) => showErrorToast(error.message),
  })

  const toggleBindingMutation = useMutation({
    mutationFn: async ({
      bindingId,
      isActive,
    }: {
      bindingId: string
      isActive: boolean
    }) => {
      const res = await fetch(`${API_BASE}/api/v1/identity/bindings/${bindingId}`, {
        method: "PUT",
        headers: getAuthHeaders(),
        body: JSON.stringify({ is_active: isActive }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to toggle binding")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const assignUsersMutation = useMutation({
    mutationFn: async ({
      bindingId,
      userIds,
    }: {
      bindingId: string
      userIds: string[]
    }) => {
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
        throw new Error((err as { detail?: string }).detail || "Failed to assign users")
      }
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  const removeAssignmentMutation = useMutation({
    mutationFn: async ({
      bindingId,
      userId,
    }: {
      bindingId: string
      userId: string
    }) => {
      const res = await fetch(
        `${API_BASE}/api/v1/identity/bindings/${bindingId}/assignments/${userId}`,
        {
          method: "DELETE",
          headers: getAuthHeaders(),
        }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error((err as { detail?: string }).detail || "Failed to remove assignment")
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identity-bindings"] }),
    onError: (error: Error) => showErrorToast(error.message),
  })

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  const handleAddOpen = () => {
    setAddAgentId("")
    setAddTriggerPrompt("")
    setAddSessionMode("conversation")
    setAddPromptExamples("")
    setAddAssignedUsers([])
    setAddDialogOpen(true)
  }

  const handleAddSave = () => {
    if (!addAgentId || !addTriggerPrompt.trim()) return
    createBindingMutation.mutate({
      agentId: addAgentId,
      triggerPrompt: addTriggerPrompt.trim(),
      promptExamples: addPromptExamples.trim() || null,
      sessionMode: addSessionMode,
      assignedUserIds: addAssignedUsers.map((s) => s.userId),
    })
  }

  const handleEditOpen = (binding: IdentityAgentBinding) => {
    setEditingBinding(binding)
    setEditTriggerPrompt(binding.trigger_prompt)
    setEditPromptExamples(binding.prompt_examples ?? "")
    setEditSessionMode(binding.session_mode)
    setEditDialogOpen(true)
  }

  const handleEditSave = () => {
    if (!editingBinding) return
    updateBindingMutation.mutate({
      bindingId: editingBinding.id,
      body: {
        trigger_prompt: editTriggerPrompt.trim(),
        prompt_examples: editPromptExamples.trim() || null,
        session_mode: editSessionMode,
      },
    })
  }

  const toggleExpanded = (bindingId: string) => {
    setExpandedBindings((prev) => {
      const next = new Set(prev)
      if (next.has(bindingId)) {
        next.delete(bindingId)
      } else {
        next.add(bindingId)
      }
      return next
    })
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <UserCircle className="h-4 w-4 text-violet-500" />
            Identity Server
          </CardTitle>
          <Button size="sm" onClick={handleAddOpen}>
            <Plus className="h-3.5 w-3.5 mr-1" />
            Add Agent
          </Button>
        </div>
        <CardDescription>
          Expose your agents through your personal identity. Other users can address you by name
          and the system routes to the right agent automatically.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {isLoading ? (
          <p className="text-xs text-muted-foreground">Loading...</p>
        ) : (
          <>
            {/* ---- Binding list ---- */}
            {bindings.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No agents in your identity yet. Use "Add Agent" above to put one behind your
                identity.
              </p>
            )}

            <div className="space-y-2">
              {bindings.map((binding) => {
                const isExpanded = expandedBindings.has(binding.id)
                return (
                  <div
                    key={binding.id}
                    className={`border rounded-lg overflow-hidden ${
                      !binding.is_active ? "opacity-60 bg-muted" : ""
                    }`}
                  >
                    {/* Main row */}
                    <div className="flex items-center justify-between px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          {binding.session_mode === "building" ? (
                            <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                          ) : (
                            <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                          )}
                          <span className="font-medium text-sm">{binding.agent_name}</span>
                          {binding.is_active ? (
                            <Badge className="text-xs bg-emerald-500 hover:bg-emerald-600 shrink-0">
                              Active
                            </Badge>
                          ) : (
                            <Badge variant="destructive" className="text-xs shrink-0">
                              Inactive
                            </Badge>
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5 ml-[22px] truncate max-w-xs">
                          {binding.trigger_prompt}
                        </p>
                      </div>

                      <div className="flex items-center gap-0.5 ml-2 shrink-0">
                        {/* Toggle expand/collapse */}
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6"
                                onClick={() => toggleExpanded(binding.id)}
                              >
                                {isExpanded ? (
                                  <ChevronUp className="h-3.5 w-3.5" />
                                ) : (
                                  <ChevronDown className="h-3.5 w-3.5" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              {isExpanded ? "Hide users" : "Show users"}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>

                        <div className="h-4 w-px bg-border mx-1" />

                        {/* Active toggle */}
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="flex items-center">
                                <Switch
                                  checked={binding.is_active}
                                  onCheckedChange={(v) =>
                                    toggleBindingMutation.mutate({
                                      bindingId: binding.id,
                                      isActive: v,
                                    })
                                  }
                                  className="scale-75"
                                />
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              {binding.is_active ? "Deactivate" : "Activate"}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>

                        {/* Edit */}
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6"
                                onClick={() => handleEditOpen(binding)}
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              Edit binding
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>

                        {/* Delete */}
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
                              <AlertDialogTitle>Remove Agent from Identity</AlertDialogTitle>
                              <AlertDialogDescription>
                                This removes {binding.agent_name} from your identity and revokes
                                access for all assigned users. Existing identity sessions are not
                                affected but cannot receive new messages.
                              </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                              <AlertDialogCancel>Cancel</AlertDialogCancel>
                              <AlertDialogAction
                                onClick={() => deleteBindingMutation.mutate(binding.id)}
                                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                              >
                                Remove
                              </AlertDialogAction>
                            </AlertDialogFooter>
                          </AlertDialogContent>
                        </AlertDialog>
                      </div>
                    </div>

                    {/* Expanded: user assignments */}
                    {isExpanded && (
                      <div className="border-t px-3 py-2 bg-muted/30 space-y-2">
                        <p className="text-xs font-medium text-muted-foreground flex items-center gap-1">
                          <Users className="h-3 w-3" />
                          Shared with
                        </p>
                        {binding.assignments.length === 0 ? (
                          <p className="text-xs text-muted-foreground italic">
                            Not shared with any users yet.
                          </p>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {binding.assignments.map((assignment) => (
                              <span
                                key={assignment.id}
                                className={`flex items-center gap-1 text-xs px-2 py-1 rounded-full ${
                                  assignment.is_active
                                    ? "bg-secondary text-secondary-foreground"
                                    : "bg-muted text-muted-foreground line-through"
                                }`}
                              >
                                {assignment.target_user_name || assignment.target_user_email}
                                <button
                                  type="button"
                                  onClick={() =>
                                    removeAssignmentMutation.mutate({
                                      bindingId: binding.id,
                                      userId: assignment.target_user_id,
                                    })
                                  }
                                  className="hover:text-destructive transition-colors"
                                  disabled={removeAssignmentMutation.isPending}
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

          </>
        )}
      </CardContent>

      {/* ---- Add Binding Dialog ---- */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Agent to Identity</DialogTitle>
            <DialogDescription>
              Expose one of your agents behind your identity. Other users can address you by
              name and the system routes to this agent automatically.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
            <div className="space-y-2">
              <Label>Agent</Label>
              {/* Disabled until the agent list has actually landed: the
                  selector renders an empty list as "No agents available", so
                  opening it mid-fetch would state something untrue about the
                  user's agents. */}
              <button
                type="button"
                onClick={() => setAgentSelectorOpen(true)}
                disabled={isAgentsLoading || isAgentsError}
                className={cn(
                  "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-all disabled:opacity-60 disabled:cursor-not-allowed",
                  selectedAgentPreset
                    ? `${selectedAgentPreset.badgeBg} ${selectedAgentPreset.badgeText} ${selectedAgentPreset.badgeHover}`
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
              >
                <Bot className="h-3.5 w-3.5" />
                <span className="truncate max-w-[200px]">
                  {isAgentsLoading
                    ? "Loading agents..."
                    : selectedAgent?.name || "Select agent..."}
                </span>
              </button>
              {isAgentsError && (
                <p className="text-xs text-destructive">
                  Couldn't load your agents. Close this dialog and try again.
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label>Session Mode</Label>
              <Select value={addSessionMode} onValueChange={setAddSessionMode}>
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
              <Label htmlFor="add-identity-trigger">Trigger Prompt</Label>
              <Textarea
                id="add-identity-trigger"
                value={addTriggerPrompt}
                onChange={(e) => setAddTriggerPrompt(e.target.value)}
                rows={3}
                placeholder="Describe when to route to this agent (e.g. 'Handle annual report requests and financial analysis')"
              />
              <p className="text-xs text-muted-foreground">
                Used by the AI router to select this agent when someone addresses you.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="add-identity-prompt-examples">Prompt Examples (optional)</Label>
              <Textarea
                id="add-identity-prompt-examples"
                value={addPromptExamples}
                onChange={(e) => setAddPromptExamples(e.target.value)}
                rows={3}
                placeholder={"generate employee report\nprepare quarterly analysis"}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                Short example prompts. MCP clients will see these prefixed with your name (e.g., 'ask Your Name to generate employee report').
              </p>
            </div>

            {/* User assignments — held locally and sent with the create call,
                since there is no binding to assign against yet. */}
            <div className="space-y-2">
              <UserAllowlistPicker
                enabled={addDialogOpen}
                selected={addAssignedUsers}
                onAdd={(u) =>
                  setAddAssignedUsers((prev) =>
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
                  setAddAssignedUsers((prev) =>
                    prev.filter((s) => s.userId !== item.userId),
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
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleAddSave}
              disabled={
                !addAgentId ||
                !addTriggerPrompt.trim() ||
                createBindingMutation.isPending
              }
            >
              {createBindingMutation.isPending ? "Adding..." : "Add to Identity"}
            </Button>
          </DialogFooter>

          {/* Agents already in the identity are excluded — one binding per
              agent, so re-picking a bound agent would only fail server-side. */}
          <AgentSelectorDialog
            open={agentSelectorOpen}
            onOpenChange={setAgentSelectorOpen}
            onSelect={setAddAgentId}
            selectedAgentId={addAgentId}
            agents={agentOptions}
            excludeAgentIds={bindings.map((b) => b.agent_id)}
            title="Select Agent"
          />
        </DialogContent>
      </Dialog>

      {/* ---- Edit Binding Dialog ---- */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Identity Binding</DialogTitle>
            <DialogDescription>
              Update the routing configuration for{" "}
              <strong>{editingBinding?.agent_name}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-1">
            <div className="space-y-2">
              <Label>Session Mode</Label>
              <Select value={editSessionMode} onValueChange={setEditSessionMode}>
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
                value={editTriggerPrompt}
                onChange={(e) => setEditTriggerPrompt(e.target.value)}
                rows={3}
                placeholder="Describe when to route to this agent"
              />
              <p className="text-xs text-muted-foreground">
                Used by the AI router to select this agent when someone addresses you.
              </p>
            </div>
            <div className="space-y-2">
              <Label>Prompt Examples (optional)</Label>
              <Textarea
                value={editPromptExamples}
                onChange={(e) => setEditPromptExamples(e.target.value)}
                rows={3}
                placeholder={"generate employee report\nprepare quarterly analysis"}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                Short example prompts. MCP clients will see these prefixed with your name (e.g., 'ask Your Name to generate employee report').
              </p>
            </div>

            {/* User assignments */}
            <div className="space-y-2">
              <UserAllowlistPicker
                enabled={editDialogOpen}
                selected={editAssignments.map((a) => ({
                  id: a.id,
                  userId: a.target_user_id,
                  fallbackLabel:
                    a.target_user_name || a.target_user_email || undefined,
                }))}
                onAdd={(u) => {
                  if (editingBinding) {
                    assignUsersMutation.mutate({
                      bindingId: editingBinding.id,
                      userIds: [u.id],
                    })
                  }
                }}
                onRemove={(item) => {
                  if (editingBinding) {
                    removeAssignmentMutation.mutate({
                      bindingId: editingBinding.id,
                      userId: item.userId,
                    })
                  }
                }}
                isRemoving={removeAssignmentMutation.isPending}
                label={
                  <Label className="flex items-center gap-2">
                    <Users className="h-4 w-4" />
                    Shared with Users
                  </Label>
                }
                searchPlaceholder="Search users to add..."
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleEditSave}
              disabled={!editTriggerPrompt.trim() || updateBindingMutation.isPending}
            >
              {updateBindingMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
