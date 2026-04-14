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
} from "lucide-react"
import { useState } from "react"

import { UsersService } from "@/client"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
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
import { Input } from "@/components/ui/input"
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
  message_patterns: string | null
  session_mode: string
  is_active: boolean
  created_at: string
  updated_at: string
  assignments: IdentityBindingAssignment[]
}

interface UserItem {
  id: string
  email: string
  full_name: string | null
}

// ---------------------------------------------------------------------------
// IdentityServerCard
// ---------------------------------------------------------------------------

export function IdentityServerCard() {
  const { user: currentUser } = useAuth()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Expanded state per binding (show/hide user assignments)
  const [expandedBindings, setExpandedBindings] = useState<Set<string>>(new Set())

  // Edit binding dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingBinding, setEditingBinding] = useState<IdentityAgentBinding | null>(null)
  const [editTriggerPrompt, setEditTriggerPrompt] = useState("")
  const [editMessagePatterns, setEditMessagePatterns] = useState("")
  const [editSessionMode, setEditSessionMode] = useState("conversation")
  const [editUserSearchQuery, setEditUserSearchQuery] = useState("")

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

  // Users for assignment picker (edit dialog)
  const { data: usersData } = useQuery({
    queryKey: ["users-list"],
    queryFn: () => UsersService.readUsers({ limit: 200 }),
    enabled: editDialogOpen,
    staleTime: 30000,
  })

  const allUsers: UserItem[] = ((usersData as { data?: UserItem[] })?.data ?? [])
  const otherUsers = allUsers.filter((u) => u.id !== currentUser?.id)

  // Edit dialog: live binding data for real-time assignment updates
  const editBindingLive = editingBinding
    ? bindings.find((b) => b.id === editingBinding.id) ?? editingBinding
    : null
  const editAssignments = editBindingLive?.assignments ?? []
  const editAssignedUserIds = editAssignments.map((a) => a.target_user_id)
  const editFilteredUsers = otherUsers.filter(
    (u) =>
      !editAssignedUserIds.includes(u.id) &&
      (u.email.toLowerCase().includes(editUserSearchQuery.toLowerCase()) ||
        (u.full_name ?? "").toLowerCase().includes(editUserSearchQuery.toLowerCase()))
  )

  // ---------------------------------------------------------------------------
  // Mutations
  // ---------------------------------------------------------------------------

  const updateBindingMutation = useMutation({
    mutationFn: async ({
      bindingId,
      body,
    }: {
      bindingId: string
      body: {
        trigger_prompt?: string
        message_patterns?: string | null
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

  const handleEditOpen = (binding: IdentityAgentBinding) => {
    setEditingBinding(binding)
    setEditTriggerPrompt(binding.trigger_prompt)
    setEditMessagePatterns(binding.message_patterns ?? "")
    setEditSessionMode(binding.session_mode)
    setEditUserSearchQuery("")
    setEditDialogOpen(true)
  }

  const handleEditSave = () => {
    if (!editingBinding) return
    updateBindingMutation.mutate({
      bindingId: editingBinding.id,
      body: {
        trigger_prompt: editTriggerPrompt.trim(),
        message_patterns: editMessagePatterns.trim() || null,
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
        <CardTitle className="flex items-center gap-2">
          <UserCircle className="h-4 w-4 text-violet-500" />
          Identity Server
        </CardTitle>
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
                No agents in your identity yet. Add agents via the Integrations tab on each agent.
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
              <Label>Message Patterns (optional)</Label>
              <Textarea
                value={editMessagePatterns}
                onChange={(e) => setEditMessagePatterns(e.target.value)}
                rows={2}
                placeholder={"annual report *\nfinancial analysis *"}
              />
              <p className="text-xs text-muted-foreground">
                One glob-style pattern per line. Matched before AI routing.
              </p>
            </div>

            {/* User assignments */}
            <div className="space-y-2">
              <Label className="flex items-center gap-2">
                <Users className="h-4 w-4" />
                Shared with Users
              </Label>
              {editAssignments.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {editAssignments.map((assignment) => (
                    <span
                      key={assignment.id}
                      className="flex items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
                    >
                      {assignment.target_user_name || assignment.target_user_email}
                      <button
                        type="button"
                        onClick={() =>
                          editingBinding &&
                          removeAssignmentMutation.mutate({
                            bindingId: editingBinding.id,
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
              <Input
                placeholder="Search users to add..."
                value={editUserSearchQuery}
                onChange={(e) => setEditUserSearchQuery(e.target.value)}
              />
              {editUserSearchQuery && editFilteredUsers.length > 0 && (
                <div className="border rounded-md divide-y max-h-36 overflow-y-auto">
                  {editFilteredUsers.slice(0, 8).map((u) => (
                    <button
                      key={u.id}
                      type="button"
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent transition-colors"
                      onClick={() => {
                        if (editingBinding) {
                          assignUsersMutation.mutate({
                            bindingId: editingBinding.id,
                            userIds: [u.id],
                          })
                        }
                        setEditUserSearchQuery("")
                      }}
                    >
                      <span className="font-medium">{u.full_name || u.email}</span>
                      {u.full_name && (
                        <span className="text-muted-foreground text-xs">{u.email}</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
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
