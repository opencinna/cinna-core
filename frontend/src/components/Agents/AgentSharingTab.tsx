import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { Share2, Info, Link2Off, Plus, Trash2, Loader2, User, Wrench, RotateCcw, Key, MessageCircle, Send, FolderSync, RefreshCw, Clock, CheckCircle2, AlertCircle, XCircle } from "lucide-react"

import type { AgentPublic, AgentSharePublic } from "@/client"
import { AgentSharesService, AiCredentialsService, AgentsService } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"

import { RevokeShareDialog } from "./ShareManagement/RevokeShareDialog"
import { UpdateModeToggle, DetachDialog, PushUpdatesModal } from "./CloneManagement"

// SDK to credential type mapping
const SDK_TO_CRED_TYPE: Record<string, string> = {
  "claude-code/anthropic": "anthropic",
  "claude-code/minimax": "minimax",
  "google-adk-wr/openai-compatible": "openai_compatible",
}

interface AgentSharingTabProps {
  agent: AgentPublic
}

export function AgentSharingTab({ agent }: AgentSharingTabProps) {
  const [detachDialogOpen, setDetachDialogOpen] = useState(false)
  const [showAddForm, setShowAddForm] = useState(false)
  const [showPushUpdatesModal, setShowPushUpdatesModal] = useState(false)
  const [revokeShare, setRevokeShare] = useState<AgentSharePublic | null>(null)
  const [removeShare, setRemoveShare] = useState<AgentSharePublic | null>(null)
  const [reshareShare, setReshareShare] = useState<AgentSharePublic | null>(null)
  const [newShareEmail, setNewShareEmail] = useState("")
  const [newShareMode, setNewShareMode] = useState<"user" | "builder">("user")
  const [provideAiCredentials, setProvideAiCredentials] = useState(false)
  const [conversationCredentialId, setConversationCredentialId] = useState<string | null>(null)
  const [buildingCredentialId, setBuildingCredentialId] = useState<string | null>(null)

  const queryClient = useQueryClient()

  // Fetch shares for owners
  const { data: shares } = useQuery({
    queryKey: ["agentShares", agent.id],
    queryFn: () => AgentSharesService.getAgentShares({ agentId: agent.id }),
    enabled: !agent.is_clone,
  })

  // Fetch pending update requests for clones
  const { data: updateRequests } = useQuery({
    queryKey: ["updateRequests", agent.id],
    queryFn: () => AgentSharesService.getPendingUpdateRequests({ agentId: agent.id }),
    enabled: agent.is_clone && agent.pending_update,
  })

  // Fetch clones for owners (to show sync status)
  const { data: clones } = useQuery({
    queryKey: ["agentClones", agent.id],
    queryFn: () => AgentSharesService.getAgentClones({ agentId: agent.id }),
    enabled: !agent.is_clone,
  })

  // Fetch agent's environment to get SDK types
  const { data: environments } = useQuery({
    queryKey: ["environments", agent.id],
    queryFn: () => AgentsService.listAgentEnvironments({ id: agent.id }),
    enabled: !agent.is_clone && provideAiCredentials,
  })

  // Get active environment's SDK types
  const activeEnv = environments?.data.find((e: { id: string }) => e.id === agent.active_environment_id)
  const conversationSdkType = activeEnv?.agent_sdk_conversation
    ? SDK_TO_CRED_TYPE[activeEnv.agent_sdk_conversation]
    : null
  const buildingSdkType = activeEnv?.agent_sdk_building
    ? SDK_TO_CRED_TYPE[activeEnv.agent_sdk_building]
    : null

  // Fetch AI credentials when providing
  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: !agent.is_clone && provideAiCredentials,
  })

  // Filter credentials by SDK type
  const conversationCredentials = aiCredentials?.data.filter(
    c => c.type === conversationSdkType
  ) || []
  const buildingCredentials = aiCredentials?.data.filter(
    c => c.type === buildingSdkType
  ) || []

  // Create share mutation
  const shareMutation = useMutation({
    mutationFn: () =>
      AgentSharesService.shareAgent({
        agentId: agent.id,
        requestBody: {
          shared_with_email: newShareEmail,
          share_mode: newShareMode,
          provide_ai_credentials: provideAiCredentials,
          conversation_ai_credential_id: provideAiCredentials ? (conversationCredentialId || undefined) : undefined,
          building_ai_credential_id: provideAiCredentials ? (buildingCredentialId || undefined) : undefined,
        },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agentShares", agent.id] })
      setNewShareEmail("")
      setNewShareMode("user")
      setProvideAiCredentials(false)
      setConversationCredentialId(null)
      setBuildingCredentialId(null)
      setShowAddForm(false)
    },
  })


  // Remove share record mutation (for deleted/declined/revoked shares cleanup)
  const removeShareMutation = useMutation({
    mutationFn: (shareId: string) =>
      AgentSharesService.revokeShare({ agentId: agent.id, shareId, action: "remove" }),
    onSuccess: () => {
      setRemoveShare(null)
      queryClient.invalidateQueries({ queryKey: ["agentShares", agent.id] })
    },
  })

  // Re-share mutation (for revoked shares)
  const reshareMutation = useMutation({
    mutationFn: (data: { email: string; mode: string }) =>
      AgentSharesService.shareAgent({
        agentId: agent.id,
        requestBody: {
          shared_with_email: data.email,
          share_mode: data.mode,
        },
      }),
    onSuccess: () => {
      setReshareShare(null)
      queryClient.invalidateQueries({ queryKey: ["agentShares", agent.id] })
    },
  })

  const handleAddShare = (e: React.FormEvent) => {
    e.preventDefault()
    shareMutation.mutate()
  }

  const getStatusBadge = (status: string) => {
    switch (status) {
      case "pending":
        return <Badge variant="outline">Pending</Badge>
      case "accepted":
        return <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">Accepted</Badge>
      case "declined":
        return <Badge variant="secondary">Declined</Badge>
      case "revoked":
        return <Badge variant="destructive">Revoked</Badge>
      case "deleted":
        return <Badge className="bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200">Deleted</Badge>
      default:
        return <Badge>{status}</Badge>
    }
  }

  const allShares = shares?.data || []

  const pendingRequests = updateRequests?.data || []

  // Create a map of clone ID to clone for quick lookup
  const cloneMap = new Map(
    (clones || []).map((clone) => [clone.id, clone])
  )

  // If this is a clone, show clone settings
  if (agent.is_clone) {
    return (
      <div className="space-y-6">
        {/* Pending update requests section */}
        {pendingRequests.length > 0 && (
          <Card className="border-blue-200 bg-blue-50/50 dark:border-blue-800 dark:bg-blue-950/20">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-blue-700 dark:text-blue-300">
                <Clock className="h-5 w-5" />
                Pending Update Requests
              </CardTitle>
              <CardDescription>
                The owner has pushed updates that are waiting to be applied.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {pendingRequests.map((request) => (
                <div
                  key={request.id}
                  className="p-3 border rounded-lg bg-white dark:bg-gray-900"
                >
                  <div className="flex items-start justify-between">
                    <div className="space-y-2">
                      <div className="text-sm">
                        <span className="text-muted-foreground">Pushed by: </span>
                        <span className="font-medium">{request.pushed_by_email}</span>
                      </div>
                      <div className="text-sm text-muted-foreground">
                        {new Date(request.created_at).toLocaleString()}
                      </div>
                      {(request.copy_files_folder || request.rebuild_environment) && (
                        <div className="flex gap-2 pt-1">
                          {request.copy_files_folder && (
                            <Badge variant="secondary" className="flex items-center gap-1">
                              <FolderSync className="h-3 w-3" />
                              Copy Files
                            </Badge>
                          )}
                          {request.rebuild_environment && (
                            <Badge variant="secondary" className="flex items-center gap-1">
                              <RefreshCw className="h-3 w-3" />
                              Rebuild Env
                            </Badge>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
              <p className="text-sm text-muted-foreground pt-2">
                Use the "Apply Update" button in the agent header to apply these changes.
              </p>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Link2Off className="h-5 w-5" />
              Clone Settings
            </CardTitle>
            <CardDescription>
              This agent is a clone shared by {agent.shared_by_email || "another user"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Update mode toggle */}
            <UpdateModeToggle
              agentId={agent.id}
              currentMode={agent.update_mode || "manual"}
            />

            {/* Detach section */}
            <div className="pt-4 border-t">
              <div className="flex items-center justify-between">
                <div>
                  <h4 className="font-medium">Detach from Parent</h4>
                  <p className="text-sm text-muted-foreground">
                    Make this agent independent. You'll have full control but won't receive updates.
                  </p>
                </div>
                <Button
                  variant="outline"
                  onClick={() => setDetachDialogOpen(true)}
                >
                  <Link2Off className="h-4 w-4 mr-2" />
                  Detach
                </Button>
              </div>
            </div>

            {/* Parent info */}
            {agent.parent_agent_name && (
              <Alert>
                <Info className="h-4 w-4" />
                <AlertDescription>
                  Parent agent: <strong>{agent.parent_agent_name}</strong>
                </AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>

        {/* Read-only notice for user mode */}
        {agent.clone_mode === "user" && (
          <Alert>
            <Info className="h-4 w-4" />
            <AlertDescription>
              You have <strong>Conversation Access</strong> to this clone.
              Configuration tabs are read-only. You can use the agent in conversation mode.
            </AlertDescription>
          </Alert>
        )}

        {/* Detach dialog */}
        <DetachDialog
          open={detachDialogOpen}
          onOpenChange={setDetachDialogOpen}
          agentId={agent.id}
          onDetached={() => {}}
        />
      </div>
    )
  }

  // For non-clones (owners), show share management
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <Share2 className="h-5 w-5" />
                Share Agent
              </CardTitle>
              <CardDescription>
                Share this agent with other users. They'll receive a clone they can use.
              </CardDescription>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setShowPushUpdatesModal(true)}>
                <Send className="h-4 w-4 mr-2" />
                Push Updates
              </Button>
              <Button onClick={() => setShowAddForm(true)}>
                <Plus className="h-4 w-4 mr-2" />
                Share Agent
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Shares table */}
          {allShares.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              No shares yet. Click "Share agent" to share this agent with other users.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Access</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Sync</TableHead>
                  <TableHead>Shared</TableHead>
                  <TableHead className="w-[140px]">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {allShares.map((share) => (
                  <TableRow key={share.id}>
                    <TableCell className="font-medium">
                      {share.shared_with_email}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {share.share_mode === "builder" ? (
                          <Wrench className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                        ) : (
                          <User className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                        )}
                        {share.share_mode === "builder" ? "Builder" : "User"}
                      </div>
                    </TableCell>
                    <TableCell>{getStatusBadge(share.status)}</TableCell>
                    <TableCell>
                      {share.status === "accepted" && share.cloned_agent_id ? (
                        (() => {
                          const clone = cloneMap.get(share.cloned_agent_id)
                          if (!clone) return <span className="text-muted-foreground">-</span>
                          if (clone.pending_update) {
                            return (
                              <div className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                                <AlertCircle className="h-4 w-4" />
                                <span className="text-xs">Pending</span>
                              </div>
                            )
                          }
                          if (clone.last_update_status === "dismissed") {
                            return (
                              <div className="flex items-center gap-1 text-muted-foreground">
                                <XCircle className="h-4 w-4" />
                                <span className="text-xs">Dismissed</span>
                              </div>
                            )
                          }
                          return (
                            <div className="flex items-center gap-1 text-green-600 dark:text-green-400">
                              <CheckCircle2 className="h-4 w-4" />
                              <span className="text-xs">Synced</span>
                            </div>
                          )
                        })()
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(share.shared_at).toLocaleDateString()}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {share.status !== "revoked" && share.status !== "deleted" && share.status !== "declined" && (
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setRevokeShare(share)}
                            title="Revoke access"
                          >
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        )}
                        {share.status === "revoked" && (
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setReshareShare(share)}
                            title="Re-share with this user"
                          >
                            <RotateCcw className="h-4 w-4" />
                          </Button>
                        )}
                        {(share.status === "deleted" || share.status === "declined" || share.status === "revoked") && (
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setRemoveShare(share)}
                            title="Remove from list"
                          >
                            <Trash2 className="h-4 w-4 text-muted-foreground" />
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

        </CardContent>
      </Card>

      {/* Information about sharing */}
      <Alert>
        <Info className="h-4 w-4" />
        <AlertDescription>
          <strong>How sharing works:</strong>
          <ul className="list-disc list-inside mt-2 space-y-1">
            <li>Recipients get a clone they can customize (if Builder mode) or use (if User mode)</li>
            <li>You can push updates to all clones when you make changes</li>
            <li>Clones can choose automatic or manual updates</li>
            <li>You can revoke access at any time (delete clone or let them keep it)</li>
          </ul>
        </AlertDescription>
      </Alert>

      {/* Add share dialog */}
      <Dialog open={showAddForm} onOpenChange={setShowAddForm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Share Agent</DialogTitle>
            <DialogDescription>
              Share this agent with another user. They'll receive a clone they can use.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleAddShare} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email">User Email</Label>
              <Input
                id="email"
                type="email"
                placeholder="user@example.com"
                value={newShareEmail}
                onChange={(e) => setNewShareEmail(e.target.value)}
                required
              />
              <p className="text-xs text-muted-foreground">
                The user must have an existing account.
              </p>
            </div>

            <div className="space-y-3">
              <Label>Access Level</Label>
              <RadioGroup value={newShareMode} onValueChange={(v) => {
                setNewShareMode(v as "user" | "builder")
                // Clear building credential when switching to user mode
                if (v === "user") {
                  setBuildingCredentialId(null)
                }
              }}>
                <label
                  htmlFor="share-user"
                  className={`flex items-start gap-3 p-3 border rounded-lg cursor-pointer transition-colors ${
                    newShareMode === "user"
                      ? "border-blue-500 bg-blue-50 dark:bg-blue-950/20"
                      : "hover:bg-muted/50"
                  }`}
                >
                  <RadioGroupItem value="user" id="share-user" className="sr-only" />
                  <MessageCircle className="h-4 w-4 text-blue-500 mt-0.5 shrink-0" />
                  <div className="flex-1">
                    <span className="font-medium">Conversation Access</span>
                    <p className="text-sm text-muted-foreground">
                      Can use the agent in conversation mode only.
                    </p>
                  </div>
                </label>
                <label
                  htmlFor="share-builder"
                  className={`flex items-start gap-3 p-3 border rounded-lg cursor-pointer transition-colors ${
                    newShareMode === "builder"
                      ? "border-orange-500 bg-orange-50 dark:bg-orange-950/20"
                      : "hover:bg-muted/50"
                  }`}
                >
                  <RadioGroupItem value="builder" id="share-builder" className="sr-only" />
                  <Wrench className="h-4 w-4 text-orange-500 mt-0.5 shrink-0" />
                  <div className="flex-1">
                    <span className="font-medium">Builder Access</span>
                    <p className="text-sm text-muted-foreground">
                      Full access to modify prompts, scripts, and configuration.
                    </p>
                  </div>
                </label>
              </RadioGroup>
            </div>

            {/* AI Credentials Provision */}
            <div className="border-t pt-4 space-y-4">
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label htmlFor="provide-ai-credentials" className="flex items-center gap-2">
                    <Key className="h-4 w-4" />
                    Provide AI Credentials
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    {provideAiCredentials
                      ? "Recipient will use your AI credentials"
                      : "Recipient must use their own AI credentials"}
                  </p>
                </div>
                <Switch
                  id="provide-ai-credentials"
                  checked={provideAiCredentials}
                  onCheckedChange={setProvideAiCredentials}
                />
              </div>

              {provideAiCredentials && (
                <div className="space-y-4 pl-4 border-l-2 border-muted">
                  {conversationSdkType && (
                    <div className="space-y-2">
                      <Label htmlFor="share-conv-cred">Conversation AI Credential</Label>
                      <Select
                        value={conversationCredentialId || ""}
                        onValueChange={(v) => setConversationCredentialId(v || null)}
                      >
                        <SelectTrigger id="share-conv-cred">
                          <SelectValue placeholder="Select credential..." />
                        </SelectTrigger>
                        <SelectContent>
                          {conversationCredentials.length === 0 ? (
                            <div className="py-2 px-2 text-sm text-muted-foreground">
                              No {conversationSdkType} credentials found
                            </div>
                          ) : (
                            conversationCredentials.map((cred) => (
                              <SelectItem key={cred.id} value={cred.id}>
                                {cred.name} {cred.is_default && "(default)"}
                              </SelectItem>
                            ))
                          )}
                        </SelectContent>
                      </Select>
                    </div>
                  )}

                  {newShareMode === "builder" && buildingSdkType && buildingSdkType !== conversationSdkType && (
                    <div className="space-y-2">
                      <Label htmlFor="share-build-cred">Building AI Credential</Label>
                      <Select
                        value={buildingCredentialId || ""}
                        onValueChange={(v) => setBuildingCredentialId(v || null)}
                      >
                        <SelectTrigger id="share-build-cred">
                          <SelectValue placeholder="Select credential..." />
                        </SelectTrigger>
                        <SelectContent>
                          {buildingCredentials.length === 0 ? (
                            <div className="py-2 px-2 text-sm text-muted-foreground">
                              No {buildingSdkType} credentials found
                            </div>
                          ) : (
                            buildingCredentials.map((cred) => (
                              <SelectItem key={cred.id} value={cred.id}>
                                {cred.name} {cred.is_default && "(default)"}
                              </SelectItem>
                            ))
                          )}
                        </SelectContent>
                      </Select>
                    </div>
                  )}

                  {!activeEnv && (
                    <p className="text-sm text-muted-foreground">
                      Loading environment configuration...
                    </p>
                  )}
                </div>
              )}
            </div>

            {shareMutation.error && (
              <Alert variant="destructive">
                <AlertDescription>
                  {(shareMutation.error as Error).message || "Failed to share agent"}
                </AlertDescription>
              </Alert>
            )}

            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => setShowAddForm(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={shareMutation.isPending || !newShareEmail}>
                {shareMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Sharing...
                  </>
                ) : (
                  "Share"
                )}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      {/* Revoke dialog */}
      {revokeShare && (
        <RevokeShareDialog
          open={!!revokeShare}
          onOpenChange={(open) => !open && setRevokeShare(null)}
          share={revokeShare}
          agentId={agent.id}
          onRevoked={() => {
            setRevokeShare(null)
            queryClient.invalidateQueries({ queryKey: ["agentShares", agent.id] })
          }}
        />
      )}

      {/* Remove share confirmation dialog */}
      <Dialog open={!!removeShare} onOpenChange={(open) => !open && setRemoveShare(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove Share Record</DialogTitle>
            <DialogDescription>
              Remove the share record for {removeShare?.shared_with_email}?
            </DialogDescription>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            This will permanently remove the share record from your list.
            This action cannot be undone.
          </p>
          {removeShareMutation.error && (
            <Alert variant="destructive">
              <AlertDescription>
                {(removeShareMutation.error as Error).message || "Failed to remove share"}
              </AlertDescription>
            </Alert>
          )}
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setRemoveShare(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => removeShare && removeShareMutation.mutate(removeShare.id)}
              disabled={removeShareMutation.isPending}
            >
              {removeShareMutation.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Removing...
                </>
              ) : (
                "Remove"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Re-share confirmation dialog */}
      <Dialog open={!!reshareShare} onOpenChange={(open) => !open && setReshareShare(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Re-share Agent</DialogTitle>
            <DialogDescription>
              Re-share this agent with {reshareShare?.shared_with_email}?
            </DialogDescription>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            This will send a new share invitation to the user.
            They will need to accept it to receive a new clone.
          </p>
          {reshareMutation.error && (
            <Alert variant="destructive">
              <AlertDescription>
                {(reshareMutation.error as Error).message || "Failed to re-share agent"}
              </AlertDescription>
            </Alert>
          )}
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setReshareShare(null)}>
              Cancel
            </Button>
            <Button
              onClick={() => reshareShare && reshareMutation.mutate({
                email: reshareShare.shared_with_email,
                mode: reshareShare.share_mode
              })}
              disabled={reshareMutation.isPending}
            >
              {reshareMutation.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Sharing...
                </>
              ) : (
                "Re-share"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Push updates modal */}
      <PushUpdatesModal
        open={showPushUpdatesModal}
        onOpenChange={setShowPushUpdatesModal}
        agentId={agent.id}
      />
    </div>
  )
}
