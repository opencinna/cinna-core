import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import {
  Check,
  Copy,
  Link2,
  Pencil,
  Plus,
  ShieldAlert,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import type {
  AgentGuestShareCreate,
  AgentGuestSharePublic,
  AgentGuestShareUpdate,
} from "@/client"
import { GuestSharesService } from "@/client"
import {
  ListRow,
  ListRowGroup,
  RowFlag,
  RowInfo,
} from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
import { Badge } from "@/components/ui/badge"
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
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

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

function getShareStatus(
  share: AgentGuestSharePublic,
): "active" | "expired" | "revoked" | "blocked" {
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
  const [createdSecurityCode, setCreatedSecurityCode] = useState<string | null>(
    null,
  )
  const [copiedUrl, setCopiedUrl] = useState(false)
  const [copiedCode, setCopiedCode] = useState(false)
  const [deleteTarget, setDeleteTarget] =
    useState<AgentGuestSharePublic | null>(null)
  const [copiedShareId, setCopiedShareId] = useState<string | null>(null)

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingShare, setEditingShare] =
    useState<AgentGuestSharePublic | null>(null)
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
      setDeleteTarget(null)
      queryClient.invalidateQueries({ queryKey: ["guest-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete guest share link")
    },
  })

  // Update guest share mutation
  const updateShareMutation = useMutation({
    mutationFn: ({
      guestShareId,
      data,
    }: {
      guestShareId: string
      data: AgentGuestShareUpdate
    }) =>
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
                  {createdShareUrl
                    ? "Share Link Created"
                    : "Create Guest Share Link"}
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
                        Share this code separately with your guest. They will
                        need it to access the link.
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
                        if (
                          e.key === "Enter" &&
                          !createShareMutation.isPending
                        ) {
                          e.preventDefault()
                          handleCreateShare()
                        }
                      }}
                    />
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="share-expiration">Expiration</Label>
                    <Select
                      value={expirationHours}
                      onValueChange={setExpirationHours}
                    >
                      <SelectTrigger id="share-expiration">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {EXPIRATION_OPTIONS.map((option) => (
                          <SelectItem
                            key={option.hours}
                            value={String(option.hours)}
                          >
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
                    {createShareMutation.isPending
                      ? "Creating..."
                      : "Create Link"}
                  </Button>
                )}
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">
            Loading share links...
          </p>
        ) : shares.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No guest share links yet. Create one to let guests chat with this
            agent.
          </p>
        ) : (
          <ListRowGroup>
            {shares.map((share: AgentGuestSharePublic) => {
              const status = getShareStatus(share)
              const isActive = status === "active"
              return (
                <ListRow
                  key={share.id}
                  muted={!isActive}
                  // Four states, one dot: the four `Badge`s this row used to
                  // print were the widest thing on it and were mutually
                  // exclusive anyway.
                  status={{
                    tone:
                      status === "active"
                        ? "on"
                        : status === "blocked"
                          ? "error"
                          : status === "expired"
                            ? "warning"
                            : "off",
                    label:
                      status === "active"
                        ? `Active — ${formatRelativeExpiry(share.expires_at)}`
                        : status === "blocked"
                          ? "Blocked — too many wrong security codes"
                          : status === "expired"
                            ? "Expired"
                            : "Revoked",
                  }}
                  title={share.label || "Untitled"}
                  // The code is the thing a publisher reads off and hands to a
                  // guest, so it stays visible — as a badge on the title line
                  // rather than a second line under it.
                  badges={
                    share.security_code ? (
                      <Badge
                        variant="outline"
                        className="h-5 shrink-0 font-mono text-xs"
                      >
                        {share.security_code}
                      </Badge>
                    ) : undefined
                  }
                  flags={
                    <>
                      {status === "blocked" && (
                        <RowFlag
                          icon={ShieldAlert}
                          tone="error"
                          label="Blocked after too many wrong security codes. Set a new code to unblock it."
                        />
                      )}
                      <RowInfo
                        facts={[
                          share.session_count === 1
                            ? "1 session created"
                            : `${share.session_count ?? 0} sessions created`,
                          share.expires_at &&
                            `${status === "expired" ? "Expired" : "Expires"} ${formatRelativeExpiry(share.expires_at)}`,
                        ]}
                      />
                    </>
                  }
                >
                  <RowActionsMenu
                    label={`the share link ${share.label || "Untitled"}`}
                  >
                    {isActive && share.share_url && (
                      <DropdownMenuItem
                        onSelect={(e) => {
                          // The item unmounts on select and would take the
                          // tick with it.
                          e.preventDefault()
                          handleCopyShareLink(share.share_url!, share.id)
                        }}
                      >
                        {copiedShareId === share.id ? <Check /> : <Copy />}
                        {copiedShareId === share.id
                          ? "Copied"
                          : "Copy share link"}
                      </DropdownMenuItem>
                    )}
                    {isActive && (
                      <DropdownMenuItem onSelect={() => handleEditOpen(share)}>
                        <Pencil />
                        Edit share
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      variant="destructive"
                      onSelect={(e) => {
                        e.preventDefault()
                        setDeleteTarget(share)
                      }}
                    >
                      <Trash2 />
                      Delete share link
                    </DropdownMenuItem>
                  </RowActionsMenu>
                </ListRow>
              )
            })}
          </ListRowGroup>
        )}
      </CardContent>

      {/* One confirm for the list, driven by the row the menu named. */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(next) => {
          if (!deleteShareMutation.isPending && !next) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete guest share link</AlertDialogTitle>
            <AlertDialogDescription>
              Delete <strong>{deleteTarget?.label || "Untitled"}</strong>? This
              cannot be undone. The link stops working, but sessions already
              created through it keep running.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteShareMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (deleteTarget) deleteShareMutation.mutate(deleteTarget.id)
              }}
              disabled={deleteShareMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteShareMutation.isPending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Guest Share</DialogTitle>
            <DialogDescription>
              Update the label or security code for this share link.
              {editingShare?.is_code_blocked && (
                <span className="block mt-1 text-destructive">
                  This link is currently blocked. Setting a new security code
                  will unblock it.
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
                Enter a new 4-digit code to replace the current one. This will
                also reset the attempt counter.
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
