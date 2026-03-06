import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import { Copy, Check, Trash2, Plus, Link2, Users, Pencil, ShieldAlert } from "lucide-react"

import type {
  AgentGuestSharePublic,
  AgentGuestShareCreate,
  AgentGuestShareUpdate,
} from "@/client"
import { GuestSharesService } from "@/client"
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
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface GuestShareCardProps {
  agentId: string
}

type ExpirationOption = {
  label: string
  hours: number
}

const EXPIRATION_OPTIONS: ExpirationOption[] = [
  { label: "1 hour", hours: 1 },
  { label: "24 hours", hours: 24 },
  { label: "7 days", hours: 168 },
  { label: "30 days", hours: 720 },
]

function getShareStatus(share: AgentGuestSharePublic): "active" | "expired" | "revoked" | "blocked" {
  if (share.is_revoked) return "revoked"
  if (new Date(share.expires_at) < new Date()) return "expired"
  if (share.is_code_blocked) return "blocked"
  return "active"
}

function formatRelativeExpiry(expiresAt: string): string {
  try {
    const ts = expiresAt.endsWith("Z") ? expiresAt : expiresAt + "Z"
    const expiry = new Date(ts)
    if (isNaN(expiry.getTime()) || expiry <= new Date()) return "Expired"
    return formatDistanceToNow(expiry) + " remaining"
  } catch {
    return "Expired"
  }
}

export function GuestShareCard({ agentId }: GuestShareCardProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [shareLabel, setShareLabel] = useState("")
  const [expirationHours, setExpirationHours] = useState<string>("24")
  const [createdShareUrl, setCreatedShareUrl] = useState<string | null>(null)
  const [createdSecurityCode, setCreatedSecurityCode] = useState<string | null>(null)
  const [copiedUrl, setCopiedUrl] = useState(false)
  const [copiedCode, setCopiedCode] = useState(false)
  const [copiedShareId, setCopiedShareId] = useState<string | null>(null)

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingShare, setEditingShare] = useState<AgentGuestSharePublic | null>(null)
  const [editLabel, setEditLabel] = useState("")
  const [editSecurityCode, setEditSecurityCode] = useState("")
  const [editAllowEnvPanel, setEditAllowEnvPanel] = useState(false)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Fetch guest shares
  const { data: sharesData, isLoading } = useQuery({
    queryKey: ["guest-shares", agentId],
    queryFn: () => GuestSharesService.listGuestShares({ agentId }),
  })

  // Create guest share mutation
  const createShareMutation = useMutation({
    mutationFn: (data: AgentGuestShareCreate) =>
      GuestSharesService.createGuestShare({
        agentId,
        requestBody: data,
      }),
    onSuccess: (response) => {
      showSuccessToast("Guest share link created successfully")
      setCreatedShareUrl(response.share_url)
      setCreatedSecurityCode(response.security_code)
      queryClient.invalidateQueries({ queryKey: ["guest-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create guest share link")
    },
  })

  // Delete guest share mutation
  const deleteShareMutation = useMutation({
    mutationFn: (guestShareId: string) =>
      GuestSharesService.deleteGuestShare({ agentId, guestShareId }),
    onSuccess: () => {
      showSuccessToast("Guest share link deleted successfully")
      queryClient.invalidateQueries({ queryKey: ["guest-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete guest share link")
    },
  })

  // Update guest share mutation
  const updateShareMutation = useMutation({
    mutationFn: ({ guestShareId, data }: { guestShareId: string; data: AgentGuestShareUpdate }) =>
      GuestSharesService.updateGuestShare({
        agentId,
        guestShareId,
        requestBody: data,
      }),
    onSuccess: () => {
      showSuccessToast("Guest share updated successfully")
      setEditDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["guest-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update guest share")
    },
  })

  const handleCreateShare = () => {
    createShareMutation.mutate({
      label: shareLabel.trim() || undefined,
      expires_in_hours: Number(expirationHours),
    })
  }

  const handleCopyShareUrl = async () => {
    if (!createdShareUrl) return
    try {
      await navigator.clipboard.writeText(createdShareUrl)
      setCopiedUrl(true)
      setTimeout(() => setCopiedUrl(false), 2000)
    } catch {
      showErrorToast("Failed to copy URL")
    }
  }

  const handleCopyCode = async () => {
    if (!createdSecurityCode) return
    try {
      await navigator.clipboard.writeText(createdSecurityCode)
      setCopiedCode(true)
      setTimeout(() => setCopiedCode(false), 2000)
    } catch {
      showErrorToast("Failed to copy code")
    }
  }

  const handleCopyShareLink = async (shareUrl: string, shareId: string) => {
    try {
      await navigator.clipboard.writeText(shareUrl)
      setCopiedShareId(shareId)
      setTimeout(() => setCopiedShareId(null), 2000)
    } catch {
      showErrorToast("Failed to copy URL")
    }
  }

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setShareLabel("")
      setExpirationHours("24")
      setCreatedShareUrl(null)
      setCreatedSecurityCode(null)
      setCopiedUrl(false)
      setCopiedCode(false)
    }
    setCreateDialogOpen(open)
  }

  const handleEditOpen = (share: AgentGuestSharePublic) => {
    setEditingShare(share)
    setEditLabel(share.label || "")
    setEditSecurityCode("")
    setEditAllowEnvPanel(share.allow_env_panel ?? false)
    setEditDialogOpen(true)
  }

  const handleEditSave = () => {
    if (!editingShare) return
    const data: AgentGuestShareUpdate = {}
    if (editLabel !== (editingShare.label || "")) {
      data.label = editLabel
    }
    if (editSecurityCode.length === 4) {
      data.security_code = editSecurityCode
    }
    if (editAllowEnvPanel !== (editingShare.allow_env_panel ?? false)) {
      data.allow_env_panel = editAllowEnvPanel
    }
    updateShareMutation.mutate({ guestShareId: editingShare.id, data })
  }

  const shares = sharesData?.data || []

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Link2 className="h-5 w-5" />
              Guest Share Links
            </CardTitle>
            <CardDescription>
              Create shareable links for guests to chat with this agent
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
                <DialogTitle>
                  {createdShareUrl ? "Share Link Created" : "Create Guest Share Link"}
                </DialogTitle>
                <DialogDescription>
                  {createdShareUrl
                    ? "Copy this link and security code, then share them with your guest."
                    : "Create a shareable link that allows guests to chat with this agent."}
                </DialogDescription>
              </DialogHeader>

              {createdShareUrl ? (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label>Share URL</Label>
                    <div className="flex gap-2">
                      <Input
                        value={createdShareUrl}
                        readOnly
                        className="font-mono text-xs"
                      />
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={handleCopyShareUrl}
                        title="Copy URL"
                      >
                        {copiedUrl ? (
                          <Check className="h-4 w-4 text-green-500" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                  </div>

                  {createdSecurityCode && (
                    <div className="space-y-2">
                      <Label>Security Code</Label>
                      <div className="flex gap-2 items-center">
                        <div className="flex-1 flex items-center justify-center gap-2 py-3 bg-muted rounded-lg">
                          {createdSecurityCode.split("").map((digit, i) => (
                            <span
                              key={i}
                              className="w-10 h-12 flex items-center justify-center text-2xl font-bold font-mono bg-background border rounded-md"
                            >
                              {digit}
                            </span>
                          ))}
                        </div>
                        <Button
                          variant="outline"
                          size="icon"
                          onClick={handleCopyCode}
                          title="Copy code"
                        >
                          {copiedCode ? (
                            <Check className="h-4 w-4 text-green-500" />
                          ) : (
                            <Copy className="h-4 w-4" />
                          )}
                        </Button>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Share this code separately with your guest. They will need it to access the link.
                      </p>
                    </div>
                  )}
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="share-label">Label (optional)</Label>
                    <Input
                      id="share-label"
                      placeholder="e.g., Demo for client X"
                      value={shareLabel}
                      onChange={(e) => setShareLabel(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !createShareMutation.isPending) {
                          e.preventDefault()
                          handleCreateShare()
                        }
                      }}
                    />
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="share-expiration">Expiration</Label>
                    <Select value={expirationHours} onValueChange={setExpirationHours}>
                      <SelectTrigger id="share-expiration">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {EXPIRATION_OPTIONS.map((option) => (
                          <SelectItem key={option.hours} value={String(option.hours)}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      The link will expire after this duration
                    </p>
                  </div>
                </div>
              )}

              <DialogFooter>
                {createdShareUrl ? (
                  <Button onClick={() => handleDialogClose(false)}>Done</Button>
                ) : (
                  <Button
                    onClick={handleCreateShare}
                    disabled={createShareMutation.isPending}
                  >
                    {createShareMutation.isPending ? "Creating..." : "Create Link"}
                  </Button>
                )}
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading share links...</p>
        ) : shares.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No guest share links yet. Create one to let guests chat with this agent.
          </p>
        ) : (
          <div className="space-y-1.5">
            {shares.map((share: AgentGuestSharePublic) => {
              const status = getShareStatus(share)
              return (
                <div
                  key={share.id}
                  className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                    status !== "active" ? "opacity-50 bg-muted" : ""
                  }`}
                >
                  {/* Left: label, status badge, code, blocked */}
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium text-sm truncate">
                      {share.label || "Untitled"}
                    </span>
                    {status === "active" && (
                      <Badge variant="default" className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                        Active
                      </Badge>
                    )}
                    {status === "expired" && (
                      <Badge variant="destructive" className="text-xs shrink-0">
                        Expired
                      </Badge>
                    )}
                    {status === "revoked" && (
                      <Badge variant="secondary" className="text-xs shrink-0">
                        Revoked
                      </Badge>
                    )}
                    {status === "blocked" && (
                      <Badge variant="destructive" className="text-xs shrink-0 flex items-center gap-1">
                        <ShieldAlert className="h-3 w-3" />
                        Blocked
                      </Badge>
                    )}
                    {share.security_code && (
                      <span className="font-mono text-xs text-muted-foreground shrink-0">
                        Code: {share.security_code}
                      </span>
                    )}
                  </div>
                  {/* Right: session count, expiry, and actions */}
                  <div className="flex items-center gap-2 shrink-0">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="flex items-center gap-1 cursor-help">
                            <Users className="h-3.5 w-3.5 text-muted-foreground" />
                            <span className="text-xs text-muted-foreground">
                              {share.session_count ?? 0}
                            </span>
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          {share.session_count === 1
                            ? "1 session created"
                            : `${share.session_count ?? 0} sessions created`}
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    {status === "active" && (
                      <span className="text-xs text-muted-foreground">
                        {formatRelativeExpiry(share.expires_at)}
                      </span>
                    )}
                    <div className="flex items-center gap-0.5 ml-1 border-l pl-2">
                      {status === "active" && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6"
                                onClick={() => handleEditOpen(share)}
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              Edit share
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                      {share.share_url && status === "active" && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-6 w-6"
                                onClick={() => handleCopyShareLink(share.share_url!, share.id)}
                              >
                                {copiedShareId === share.id ? (
                                  <Check className="h-3.5 w-3.5 text-green-500" />
                                ) : (
                                  <Copy className="h-3.5 w-3.5" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              Copy share link
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
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
                            <AlertDialogTitle>Delete Guest Share Link</AlertDialogTitle>
                            <AlertDialogDescription>
                              Are you sure you want to delete the share link
                              &ldquo;{share.label || "Untitled"}&rdquo;?
                              This action cannot be undone. The link will no longer be
                              usable, but existing sessions will continue to work.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                              onClick={() => deleteShareMutation.mutate(share.id)}
                              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                            >
                              Delete
                            </AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      </AlertDialog>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Guest Share</DialogTitle>
            <DialogDescription>
              Update the label or security code for this share link.
              {editingShare?.is_code_blocked && (
                <span className="block mt-1 text-destructive">
                  This link is currently blocked. Setting a new security code will unblock it.
                </span>
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="edit-label">Label</Label>
              <Input
                id="edit-label"
                placeholder="e.g., Demo for client X"
                value={editLabel}
                onChange={(e) => setEditLabel(e.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="edit-code">
                New Security Code
                {editingShare?.security_code && (
                  <span className="font-normal text-muted-foreground ml-2">
                    (current: {editingShare.security_code})
                  </span>
                )}
              </Label>
              <Input
                id="edit-code"
                placeholder="4-digit code (leave empty to keep current)"
                value={editSecurityCode}
                onChange={(e) => {
                  const val = e.target.value.replace(/\D/g, "").slice(0, 4)
                  setEditSecurityCode(val)
                }}
                maxLength={4}
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                Enter a new 4-digit code to replace the current one. This will also reset the attempt counter.
              </p>
            </div>

            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="edit-env-panel">Show App panel</Label>
                <p className="text-xs text-muted-foreground">
                  Allow guests to access the agent's environment panel
                </p>
              </div>
              <Switch
                id="edit-env-panel"
                checked={editAllowEnvPanel}
                onCheckedChange={setEditAllowEnvPanel}
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleEditSave}
              disabled={updateShareMutation.isPending}
            >
              {updateShareMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
