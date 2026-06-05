import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Copy,
  Check,
  Trash2,
  Plus,
  Eye,
  EyeOff,
  Info,
  Wrench,
  MessageCircle,
  Lock,
  Globe,
} from "lucide-react"

import type {
  AgentAccessTokenPublic,
  AgentAccessTokenCreate,
  AccessTokenMode,
  AccessTokenScope,
} from "@/client"
import { AccessTokensService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface A2aAccessTokensManagerProps {
  agentId: string
}

/**
 * A2A access-token list + create dialog. Presentational/management subcomponent
 * embedded inside the A2A Integration card (no Card wrapper of its own).
 * Backed by the existing AccessTokensService (A2A JWT tokens).
 */
export function A2aAccessTokensManager({ agentId }: A2aAccessTokensManagerProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [tokenName, setTokenName] = useState("")
  const [tokenMode, setTokenMode] = useState<AccessTokenMode>("conversation")
  const [tokenScope, setTokenScope] = useState<AccessTokenScope>("limited")
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [copiedToken, setCopiedToken] = useState(false)
  const [showToken, setShowToken] = useState(false)

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
      queryClient.invalidateQueries({ queryKey: ["access-tokens", agentId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete access token")
    },
  })

  const revokeTokenMutation = useMutation({
    mutationFn: ({ tokenId, isRevoked }: { tokenId: string; isRevoked: boolean }) =>
      AccessTokensService.updateAccessToken({
        agentId,
        tokenId,
        requestBody: { is_revoked: isRevoked },
      }),
    onSuccess: (_, { isRevoked }) => {
      showSuccessToast(isRevoked ? "Access token revoked" : "Access token restored")
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
                      <SelectItem value="conversation">Conversation Only</SelectItem>
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
                      <SelectItem value="limited">Limited (own sessions only)</SelectItem>
                      <SelectItem value="general">General (all sessions)</SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    Limited: can only access sessions created by this token. General:
                    can access all agent sessions.
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
                  {createTokenMutation.isPending ? "Creating..." : "Create Token"}
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
        <div className="space-y-1.5">
          {tokens.map((token: AgentAccessTokenPublic) => (
            <div
              key={token.id}
              className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                token.is_revoked ? "opacity-50 bg-muted" : ""
              }`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-medium text-sm truncate">{token.name}</span>
                {token.is_revoked && (
                  <Badge variant="destructive" className="text-xs shrink-0">
                    Revoked
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <span className="font-mono text-xs text-muted-foreground">
                  {token.token_prefix}...
                </span>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex items-center">
                        {token.mode === "building" ? (
                          <Wrench className="h-3.5 w-3.5 text-orange-500" />
                        ) : (
                          <MessageCircle className="h-3.5 w-3.5 text-blue-500" />
                        )}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      {token.mode === "building" ? "Building mode" : "Conversation only"}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex items-center">
                        {token.scope === "limited" ? (
                          <Lock className="h-3.5 w-3.5 text-amber-500" />
                        ) : (
                          <Globe className="h-3.5 w-3.5 text-green-500" />
                        )}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      {token.scope === "limited"
                        ? "Limited (own sessions)"
                        : "General (all sessions)"}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex items-center cursor-help">
                        <Info className="h-3.5 w-3.5 text-muted-foreground" />
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      <div className="space-y-1">
                        <p>Created: {formatDate(token.created_at)}</p>
                        {token.last_used_at && (
                          <p>Last used: {formatDate(token.last_used_at)}</p>
                        )}
                      </div>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <div className="flex items-center gap-0.5 ml-1 border-l pl-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={() =>
                      revokeTokenMutation.mutate({
                        tokenId: token.id,
                        isRevoked: !token.is_revoked,
                      })
                    }
                    disabled={revokeTokenMutation.isPending}
                  >
                    {token.is_revoked ? "Restore" : "Revoke"}
                  </Button>
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
                        <AlertDialogTitle>Delete Access Token</AlertDialogTitle>
                        <AlertDialogDescription>
                          Are you sure you want to delete the token "{token.name}"? This
                          action cannot be undone. Any systems using this token will lose
                          access to this agent.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          onClick={() => deleteTokenMutation.mutate(token.id)}
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
          ))}
        </div>
      )}
    </div>
  )
}
