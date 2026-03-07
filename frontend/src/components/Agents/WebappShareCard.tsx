import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import {
  Copy,
  Check,
  Trash2,
  Plus,
  Globe,
  Pencil,
  ShieldAlert,
  Code2,
} from "lucide-react"

import type {
  AgentWebappSharePublic,
  AgentWebappShareCreate,
  AgentWebappShareUpdate,
} from "@/client"
import { AgentsService, WebappSharesService } from "@/client"
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

interface WebappShareCardProps {
  agentId: string
  webappEnabled: boolean
}

const EXPIRATION_OPTIONS = [
  { label: "1 hour", hours: 1 },
  { label: "24 hours", hours: 24 },
  { label: "7 days", hours: 168 },
  { label: "30 days", hours: 720 },
  { label: "Unlimited", hours: 0 },
]

function getShareStatus(
  share: AgentWebappSharePublic
): "active" | "expired" | "inactive" | "blocked" {
  if (!share.is_active) return "inactive"
  if (share.expires_at && new Date(share.expires_at) < new Date()) return "expired"
  if (share.is_code_blocked) return "blocked"
  return "active"
}

function formatRelativeExpiry(expiresAt: string | null): string {
  if (!expiresAt) return "No expiration"
  try {
    const ts = expiresAt.endsWith("Z") ? expiresAt : expiresAt + "Z"
    const expiry = new Date(ts)
    if (isNaN(expiry.getTime()) || expiry <= new Date()) return "Expired"
    return formatDistanceToNow(expiry) + " remaining"
  } catch {
    return "Expired"
  }
}

export function WebappShareCard({
  agentId,
  webappEnabled,
}: WebappShareCardProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [shareLabel, setShareLabel] = useState("")
  const [expirationHours, setExpirationHours] = useState<string>("168")
  const [allowDataApi, setAllowDataApi] = useState(true)
  const [requireSecurityCode, setRequireSecurityCode] = useState(false)
  const [createdShareUrl, setCreatedShareUrl] = useState<string | null>(null)
  const [createdSecurityCode, setCreatedSecurityCode] = useState<string | null>(
    null
  )
  const [copiedUrl, setCopiedUrl] = useState(false)
  const [copiedCode, setCopiedCode] = useState(false)
  const [copiedEmbed, setCopiedEmbed] = useState(false)
  const [copiedShareId, setCopiedShareId] = useState<string | null>(null)
  const [copiedEmbedShareId, setCopiedEmbedShareId] = useState<string | null>(
    null
  )

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingShare, setEditingShare] = useState<AgentWebappSharePublic | null>(null)
  const [editLabel, setEditLabel] = useState("")
  const [editSecurityCode, setEditSecurityCode] = useState("")
  const [editAllowDataApi, setEditAllowDataApi] = useState(true)
  const [editRequireCode, setEditRequireCode] = useState(false)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: sharesData, isLoading } = useQuery({
    queryKey: ["webapp-shares", agentId],
    queryFn: () => WebappSharesService.listWebappShares({ agentId }),
    enabled: webappEnabled,
  })

  const createShareMutation = useMutation({
    mutationFn: (data: AgentWebappShareCreate) =>
      WebappSharesService.createWebappShare({ agentId, requestBody: data }),
    onSuccess: (response) => {
      showSuccessToast("Webapp share link created")
      setCreatedShareUrl(response.share_url)
      setCreatedSecurityCode(response.security_code)
      queryClient.invalidateQueries({ queryKey: ["webapp-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create webapp share link")
    },
  })

  const deleteShareMutation = useMutation({
    mutationFn: (shareId: string) =>
      WebappSharesService.deleteWebappShare({ agentId, shareId }),
    onSuccess: () => {
      showSuccessToast("Webapp share link deleted")
      queryClient.invalidateQueries({ queryKey: ["webapp-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete webapp share link")
    },
  })

  const updateShareMutation = useMutation({
    mutationFn: ({
      shareId,
      data,
    }: {
      shareId: string
      data: AgentWebappShareUpdate
    }) =>
      WebappSharesService.updateWebappShare({
        agentId,
        shareId,
        requestBody: data,
      }),
    onSuccess: () => {
      showSuccessToast("Webapp share updated")
      setEditDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["webapp-shares", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update webapp share")
    },
  })

  const handleCreateShare = () => {
    createShareMutation.mutate({
      label: shareLabel.trim() || undefined,
      expires_in_hours: Number(expirationHours) || undefined,
      allow_data_api: allowDataApi,
      require_security_code: requireSecurityCode,
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

  const handleCopyEmbed = async () => {
    if (!createdShareUrl) return
    const snippet = `<iframe src="${createdShareUrl}?embed=1" width="100%" height="600" style="border:none;"></iframe>`
    try {
      await navigator.clipboard.writeText(snippet)
      setCopiedEmbed(true)
      setTimeout(() => setCopiedEmbed(false), 2000)
    } catch {
      showErrorToast("Failed to copy embed code")
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

  const handleCopyEmbedSnippet = async (
    shareUrl: string,
    shareId: string
  ) => {
    const snippet = `<iframe src="${shareUrl}?embed=1" width="100%" height="600" style="border:none;"></iframe>`
    try {
      await navigator.clipboard.writeText(snippet)
      setCopiedEmbedShareId(shareId)
      setTimeout(() => setCopiedEmbedShareId(null), 2000)
    } catch {
      showErrorToast("Failed to copy embed code")
    }
  }

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setShareLabel("")
      setExpirationHours("168")
      setAllowDataApi(true)
      setRequireSecurityCode(false)
      setCreatedShareUrl(null)
      setCreatedSecurityCode(null)
      setCopiedUrl(false)
      setCopiedCode(false)
      setCopiedEmbed(false)
    }
    setCreateDialogOpen(open)
  }

  const handleEditOpen = (share: AgentWebappSharePublic) => {
    setEditingShare(share)
    setEditLabel(share.label || "")
    setEditSecurityCode("")
    setEditAllowDataApi(share.allow_data_api ?? true)
    setEditRequireCode(!!share.security_code)
    setEditDialogOpen(true)
  }

  const handleEditSave = () => {
    if (!editingShare) return
    const data: AgentWebappShareUpdate = {}
    if (editLabel !== (editingShare.label || "")) {
      data.label = editLabel
    }
    const hadCode = !!editingShare.security_code
    if (hadCode && !editRequireCode) {
      data.remove_security_code = true
    } else if (editRequireCode && editSecurityCode.length === 4) {
      data.security_code = editSecurityCode
    }
    if (editAllowDataApi !== (editingShare.allow_data_api ?? true)) {
      data.allow_data_api = editAllowDataApi
    }
    updateShareMutation.mutate({ shareId: editingShare.id, data })
  }

  const toggleWebappMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { webapp_enabled: enabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["webapp-shares", agentId] })
      queryClient.invalidateQueries({ queryKey: ["webapp-status", agentId] })
      showSuccessToast("Web app setting updated")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update web app setting")
    },
  })

  const shares = sharesData?.data || []

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Globe className="h-5 w-5" />
              Web App
            </CardTitle>
            <CardDescription>
              {webappEnabled
                ? "Create shareable links for your agent's web app dashboard"
                : "Enable to serve and share your agent's web app dashboard"}
            </CardDescription>
          </div>
          <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
            <div className="relative">
              <input
                type="checkbox"
                checked={webappEnabled}
                onChange={(e) => toggleWebappMutation.mutate(e.target.checked)}
                disabled={toggleWebappMutation.isPending}
                className="sr-only"
              />
              <div
                className={`block h-6 w-11 rounded-full transition-colors ${
                  webappEnabled ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                }`}
              ></div>
              <div
                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  webappEnabled ? "translate-x-5" : ""
                }`}
              ></div>
            </div>
          </label>
        </div>
      </CardHeader>
      <CardContent>
        {!webappEnabled ? null : (
          <>
          <div className="flex items-center justify-between mb-3">
            <span className="text-sm font-medium">Share Links</span>
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
                    : "Create Webapp Share Link"}
                </DialogTitle>
                <DialogDescription>
                  {createdShareUrl
                    ? createdSecurityCode
                      ? "Copy this link and security code, then share them."
                      : "Copy this link and share it."
                    : "Create a shareable link for the agent's web app."}
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
                        Share this code separately. It is required to access the
                        link.
                      </p>
                    </div>
                  )}

                  <div className="space-y-2">
                    <Label>Embed Code</Label>
                    <div className="flex gap-2">
                      <Input
                        value={`<iframe src="${createdShareUrl}?embed=1" width="100%" height="600" style="border:none;"></iframe>`}
                        readOnly
                        className="font-mono text-xs"
                      />
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={handleCopyEmbed}
                        title="Copy embed code"
                      >
                        {copiedEmbed ? (
                          <Check className="h-4 w-4 text-green-500" />
                        ) : (
                          <Code2 className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="webapp-share-label">Label (optional)</Label>
                    <Input
                      id="webapp-share-label"
                      placeholder="e.g., Sales Dashboard - External"
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
                    <Label htmlFor="webapp-share-expiration">Expiration</Label>
                    <Select
                      value={expirationHours}
                      onValueChange={setExpirationHours}
                    >
                      <SelectTrigger id="webapp-share-expiration">
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
                  </div>

                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="webapp-share-data-api">
                        Allow data API
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        Enable dynamic data endpoints for this share
                      </p>
                    </div>
                    <Switch
                      id="webapp-share-data-api"
                      checked={allowDataApi}
                      onCheckedChange={setAllowDataApi}
                    />
                  </div>

                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="webapp-share-security-code">
                        Require security code
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        Require a 4-digit code to access this share
                      </p>
                    </div>
                    <Switch
                      id="webapp-share-security-code"
                      checked={requireSecurityCode}
                      onCheckedChange={setRequireSecurityCode}
                    />
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
        {isLoading ? (
          <p className="text-sm text-muted-foreground">
            Loading share links...
          </p>
        ) : shares.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No webapp share links yet. Create one to share your agent's web app.
          </p>
        ) : (
          <div className="space-y-1.5">
            {shares.map((share: AgentWebappSharePublic) => {
              const status = getShareStatus(share)
              return (
                <div
                  key={share.id}
                  className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                    status !== "active" ? "opacity-50 bg-muted" : ""
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium text-sm truncate">
                      {share.label || "Untitled"}
                    </span>
                    {status === "active" && (
                      <Badge
                        variant="default"
                        className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600"
                      >
                        Active
                      </Badge>
                    )}
                    {status === "expired" && (
                      <Badge variant="destructive" className="text-xs shrink-0">
                        Expired
                      </Badge>
                    )}
                    {status === "inactive" && (
                      <Badge variant="secondary" className="text-xs shrink-0">
                        Inactive
                      </Badge>
                    )}
                    {status === "blocked" && (
                      <Badge
                        variant="destructive"
                        className="text-xs shrink-0 flex items-center gap-1"
                      >
                        <ShieldAlert className="h-3 w-3" />
                        Blocked
                      </Badge>
                    )}
                    {share.security_code && (
                      <span className="font-mono text-xs text-muted-foreground shrink-0">
                        Code: {share.security_code}
                      </span>
                    )}
                    {!share.allow_data_api && (
                      <span className="text-xs text-muted-foreground shrink-0">
                        (static only)
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {status === "active" && (
                      <span className="text-xs text-muted-foreground">
                        {formatRelativeExpiry(share.expires_at)}
                      </span>
                    )}
                    <div className="flex items-center gap-0.5 ml-1 border-l pl-2">
                      {status === "active" && (
                        <>
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
                              <TooltipContent
                                side="top"
                                className="text-xs"
                              >
                                Edit share
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                          {share.share_url && (
                            <>
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-6 w-6"
                                      onClick={() =>
                                        handleCopyShareLink(
                                          share.share_url!,
                                          share.id
                                        )
                                      }
                                    >
                                      {copiedShareId === share.id ? (
                                        <Check className="h-3.5 w-3.5 text-green-500" />
                                      ) : (
                                        <Copy className="h-3.5 w-3.5" />
                                      )}
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent
                                    side="top"
                                    className="text-xs"
                                  >
                                    Copy share link
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
                                        handleCopyEmbedSnippet(
                                          share.share_url!,
                                          share.id
                                        )
                                      }
                                    >
                                      {copiedEmbedShareId === share.id ? (
                                        <Check className="h-3.5 w-3.5 text-green-500" />
                                      ) : (
                                        <Code2 className="h-3.5 w-3.5" />
                                      )}
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent
                                    side="top"
                                    className="text-xs"
                                  >
                                    Copy embed code
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            </>
                          )}
                        </>
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
                            <AlertDialogTitle>
                              Delete Webapp Share Link
                            </AlertDialogTitle>
                            <AlertDialogDescription>
                              Are you sure you want to delete the share link
                              &ldquo;{share.label || "Untitled"}&rdquo;? This
                              action cannot be undone.
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>Cancel</AlertDialogCancel>
                            <AlertDialogAction
                              onClick={() =>
                                deleteShareMutation.mutate(share.id)
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
                </div>
              )
            })}
          </div>
        )}

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Web App Share</DialogTitle>
            <DialogDescription>
              Update the label, security code, or data API access.
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
              <Label htmlFor="edit-webapp-label">Label</Label>
              <Input
                id="edit-webapp-label"
                placeholder="e.g., Sales Dashboard - External"
                value={editLabel}
                onChange={(e) => setEditLabel(e.target.value)}
              />
            </div>

            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="edit-webapp-data-api">Allow data API</Label>
                <p className="text-xs text-muted-foreground">
                  Enable dynamic data endpoints for this share
                </p>
              </div>
              <Switch
                id="edit-webapp-data-api"
                checked={editAllowDataApi}
                onCheckedChange={setEditAllowDataApi}
              />
            </div>

            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="edit-webapp-require-code">
                  Require security code
                </Label>
                <p className="text-xs text-muted-foreground">
                  Require a 4-digit code to access this share
                </p>
              </div>
              <Switch
                id="edit-webapp-require-code"
                checked={editRequireCode}
                onCheckedChange={setEditRequireCode}
              />
            </div>

            {editRequireCode && (
              <div className="space-y-2">
                <Label htmlFor="edit-webapp-code">
                  {editingShare?.security_code ? "Change Security Code" : "Security Code"}
                  {editingShare?.security_code && (
                    <span className="font-normal text-muted-foreground ml-2">
                      (current: {editingShare.security_code})
                    </span>
                  )}
                </Label>
                <Input
                  id="edit-webapp-code"
                  placeholder={editingShare?.security_code ? "4-digit code (leave empty to keep current)" : "Enter a 4-digit code"}
                  value={editSecurityCode}
                  onChange={(e) => {
                    const val = e.target.value.replace(/\D/g, "").slice(0, 4)
                    setEditSecurityCode(val)
                  }}
                  maxLength={4}
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  {editingShare?.security_code
                    ? "Enter a new 4-digit code to replace the current one. This will also reset the attempt counter."
                    : "Enter a 4-digit code. It will be required to access the share."}
                </p>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditDialogOpen(false)}
            >
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
          </>
        )}
      </CardContent>
    </Card>
  )
}
