import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Ban,
  Check,
  Copy,
  Eye,
  EyeOff,
  Globe,
  Lock,
  MessageCircle,
  Plus,
  RotateCcw,
  Trash2,
  Wrench,
} from "lucide-react"
import { useState } from "react"

import type {
  AccessTokenMode,
  AccessTokenScope,
  AgentAccessTokenCreate,
  AgentAccessTokenPublic,
} from "@/client"
import { AccessTokensService } from "@/client"
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
import { Button } from "@/components/ui/button"
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
import useCustomToast from "@/hooks/useCustomToast"

interface A2aAccessTokensManagerProps {
  agentId: string
}

/**
 * A2A access-token list + create dialog. Presentational/management subcomponent
 * embedded inside the A2A Integration card (no Card wrapper of its own).
 * Backed by the existing AccessTokensService (A2A JWT tokens).
 */
export function A2aAccessTokensManager({
  agentId,
}: A2aAccessTokensManagerProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [tokenName, setTokenName] = useState("")
  const [tokenMode, setTokenMode] = useState<AccessTokenMode>("conversation")
  const [tokenScope, setTokenScope] = useState<AccessTokenScope>("limited")
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [copiedToken, setCopiedToken] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [deleteTarget, setDeleteTarget] =
    useState<AgentAccessTokenPublic | null>(null)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: tokensData, isLoading } = useQuery({
    queryKey: ["access-tokens", agentId],
    queryFn: () => AccessTokensService.listAccessTokens({ agentId }),
  })

  const createTokenMutation = useMutation({
    mutationFn: (data: AgentAccessTokenCreate) =>
      AccessTokensService.createAccessToken({ agentId, requestBody: data }),
    onSuccess: (response) => {
      showSuccessToast("Access token created successfully")
      setCreatedToken(response.token)
      queryClient.invalidateQueries({ queryKey: ["access-tokens", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create access token")
    },
  })

  const deleteTokenMutation = useMutation({
    mutationFn: (tokenId: string) =>
      AccessTokensService.deleteAccessToken({ agentId, tokenId }),
    onSuccess: () => {
      showSuccessToast("Access token deleted successfully")
      setDeleteTarget(null)
      queryClient.invalidateQueries({ queryKey: ["access-tokens", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete access token")
    },
  })

  const revokeTokenMutation = useMutation({
    mutationFn: ({
      tokenId,
      isRevoked,
    }: {
      tokenId: string
      isRevoked: boolean
    }) =>
      AccessTokensService.updateAccessToken({
        agentId,
        tokenId,
        requestBody: { is_revoked: isRevoked },
      }),
    onSuccess: (_, { isRevoked }) => {
      showSuccessToast(
        isRevoked ? "Access token revoked" : "Access token restored",
      )
      queryClient.invalidateQueries({ queryKey: ["access-tokens", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update access token")
    },
  })

  const handleCreateToken = () => {
    if (!tokenName.trim()) {
      showErrorToast("Please enter a token name")
      return
    }
    createTokenMutation.mutate({
      agent_id: agentId,
      name: tokenName,
      mode: tokenMode,
      scope: tokenScope,
    })
  }

  const handleCopyToken = async () => {
    if (!createdToken) return
    try {
      await navigator.clipboard.writeText(createdToken)
      setCopiedToken(true)
      setTimeout(() => setCopiedToken(false), 2000)
    } catch {
      showErrorToast("Failed to copy token")
    }
  }

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setTokenName("")
      setTokenMode("conversation")
      setTokenScope("limited")
      setCreatedToken(null)
      setCopiedToken(false)
      setShowToken(false)
    }
    setCreateDialogOpen(open)
  }

  const formatDate = (dateString: string) =>
    new Date(dateString).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    })

  const tokens = tokensData?.data || []

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="space-y-0.5">
          <p className="text-sm font-medium">Access Tokens</p>
          <p className="text-xs text-muted-foreground">
            Tokens for external A2A clients to access this agent
          </p>
        </div>
        <Dialog open={createDialogOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm" variant="outline">
              <Plus className="h-4 w-4 mr-1" />
              New token
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {createdToken ? "Token Created" : "Create Access Token"}
              </DialogTitle>
              <DialogDescription>
                {createdToken
                  ? "Copy this token now. You won't be able to see it again."
                  : "Create a new access token for external A2A communication."}
              </DialogDescription>
            </DialogHeader>

            {createdToken ? (
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label>Access Token</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        value={showToken ? createdToken : "•".repeat(40)}
                        readOnly
                        className="font-mono text-xs pr-20"
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        className="absolute right-8 top-1/2 -translate-y-1/2 h-6 w-6 p-0"
                        onClick={() => setShowToken(!showToken)}
                      >
                        {showToken ? (
                          <EyeOff className="h-3 w-3" />
                        ) : (
                          <Eye className="h-3 w-3" />
                        )}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="absolute right-1 top-1/2 -translate-y-1/2 h-6 w-6 p-0"
                        onClick={handleCopyToken}
                      >
                        {copiedToken ? (
                          <Check className="h-3 w-3 text-green-500" />
                        ) : (
                          <Copy className="h-3 w-3" />
                        )}
                      </Button>
                    </div>
                  </div>
                  <p className="text-xs text-amber-600 dark:text-amber-500">
                    Store this token securely. It cannot be retrieved later.
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="a2a-token-name">Name</Label>
                  <Input
                    id="a2a-token-name"
                    placeholder="e.g., Production API, External Client"
                    value={tokenName}
                    onChange={(e) => setTokenName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !createTokenMutation.isPending) {
                        e.preventDefault()
                        handleCreateToken()
                      }
                    }}
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="a2a-token-mode">Mode</Label>
                  <Select
                    value={tokenMode}
                    onValueChange={(v) => setTokenMode(v as AccessTokenMode)}
                  >
                    <SelectTrigger id="a2a-token-mode">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="conversation">
                        Conversation Only
                      </SelectItem>
                      <SelectItem value="building">
                        Building (includes Conversation)
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    Controls which agent modes the token can access
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="a2a-token-scope">Scope</Label>
                  <Select
                    value={tokenScope}
                    onValueChange={(v) => setTokenScope(v as AccessTokenScope)}
                  >
                    <SelectTrigger id="a2a-token-scope">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="limited">
                        Limited (own sessions only)
                      </SelectItem>
                      <SelectItem value="general">
                        General (all sessions)
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    Limited: can only access sessions created by this token.
                    General: can access all agent sessions.
                  </p>
                </div>
              </div>
            )}

            <DialogFooter>
              {createdToken ? (
                <Button onClick={() => handleDialogClose(false)}>Done</Button>
              ) : (
                <Button
                  onClick={handleCreateToken}
                  disabled={createTokenMutation.isPending}
                >
                  {createTokenMutation.isPending
                    ? "Creating..."
                    : "Create Token"}
                </Button>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading tokens...</p>
      ) : tokens.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No access tokens yet. Create one to enable external A2A access.
        </p>
      ) : (
        <ListRowGroup>
          {tokens.map((token: AgentAccessTokenPublic) => (
            <ListRow
              key={token.id}
              muted={token.is_revoked}
              // Revoked was a `Badge`; it is the dot now, like every other
              // list in the product.
              status={{
                tone: token.is_revoked ? "error" : "on",
                label: token.is_revoked ? "Revoked" : "Active",
              }}
              title={token.name}
              flags={
                <>
                  <RowFlag
                    icon={token.mode === "building" ? Wrench : MessageCircle}
                    label={
                      token.mode === "building"
                        ? "Building mode — may change the agent"
                        : "Conversation only"
                    }
                    tone={token.mode === "building" ? "warning" : "neutral"}
                  />
                  <RowFlag
                    icon={token.scope === "limited" ? Lock : Globe}
                    label={
                      token.scope === "limited"
                        ? "Limited — only its own sessions"
                        : "General — all sessions of this agent"
                    }
                  />
                  <RowInfo
                    facts={[
                      `Prefix ${token.token_prefix}…`,
                      `Created ${formatDate(token.created_at)}`,
                      token.last_used_at
                        ? `Last used ${formatDate(token.last_used_at)}`
                        : "Never used",
                    ]}
                  />
                </>
              }
            >
              <RowActionsMenu
                label={`the token ${token.name}`}
                disabled={revokeTokenMutation.isPending}
              >
                <DropdownMenuItem
                  onSelect={() =>
                    revokeTokenMutation.mutate({
                      tokenId: token.id,
                      isRevoked: !token.is_revoked,
                    })
                  }
                >
                  {token.is_revoked ? <RotateCcw /> : <Ban />}
                  {token.is_revoked ? "Restore token" : "Revoke token"}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={(e) => {
                    e.preventDefault()
                    setDeleteTarget(token)
                  }}
                >
                  <Trash2 />
                  Delete token
                </DropdownMenuItem>
              </RowActionsMenu>
            </ListRow>
          ))}
        </ListRowGroup>
      )}

      {/* One confirm for the list, driven by the row the menu named: a
          per-row `AlertDialog` nested in a menu item loses its pending state
          when the item unmounts on select. */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(next) => {
          if (!deleteTokenMutation.isPending && !next) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete access token</AlertDialogTitle>
            <AlertDialogDescription>
              Delete the token <strong>{deleteTarget?.name}</strong>? This
              cannot be undone, and any system using it loses access to this
              agent.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteTokenMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (deleteTarget) deleteTokenMutation.mutate(deleteTarget.id)
              }}
              disabled={deleteTokenMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteTokenMutation.isPending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
