import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Copy, Check, Trash2, Plus, Eye, EyeOff, Info } from "lucide-react"

import type { MCPConnectorTokenPublic } from "@/client"
import { McpConnectorsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
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

interface McpDirectTokensManagerProps {
  agentId: string
  connectorId: string
}

/**
 * Direct access-token list + create dialog for an MCP connector. Connector-scoped
 * opaque bearer tokens that connect under the owner's identity without an
 * account. One-time reveal on creation. Backed by McpConnectorsService.
 */
export function McpDirectTokensManager({
  agentId,
  connectorId,
}: McpDirectTokensManagerProps) {
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [label, setLabel] = useState("")
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [copiedToken, setCopiedToken] = useState(false)
  const [showToken, setShowToken] = useState(false)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: tokensData, isLoading } = useQuery({
    queryKey: ["mcp-connector-tokens", connectorId],
    queryFn: () =>
      McpConnectorsService.listConnectorTokens({ agentId, connectorId }),
  })

  const createTokenMutation = useMutation({
    mutationFn: (newLabel: string) =>
      McpConnectorsService.createConnectorToken({
        agentId,
        connectorId,
        requestBody: { label: newLabel },
      }),
    onSuccess: (response) => {
      showSuccessToast("Direct token created")
      setCreatedToken(response.token)
      queryClient.invalidateQueries({
        queryKey: ["mcp-connector-tokens", connectorId],
      })
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || error.message || "Failed to create token")
    },
  })

  const revokeTokenMutation = useMutation({
    mutationFn: ({ tokenId, revoked }: { tokenId: string; revoked: boolean }) =>
      McpConnectorsService.updateConnectorToken({
        agentId,
        connectorId,
        tokenId,
        requestBody: { revoked },
      }),
    onSuccess: (_, { revoked }) => {
      showSuccessToast(revoked ? "Token revoked" : "Token restored")
      queryClient.invalidateQueries({
        queryKey: ["mcp-connector-tokens", connectorId],
      })
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || error.message || "Failed to update token")
    },
  })

  const deleteTokenMutation = useMutation({
    mutationFn: (tokenId: string) =>
      McpConnectorsService.deleteConnectorToken({ agentId, connectorId, tokenId }),
    onSuccess: () => {
      showSuccessToast("Token deleted")
      queryClient.invalidateQueries({
        queryKey: ["mcp-connector-tokens", connectorId],
      })
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || error.message || "Failed to delete token")
    },
  })

  const handleCreateToken = () => {
    if (!label.trim()) {
      showErrorToast("Please enter a token name")
      return
    }
    createTokenMutation.mutate(label.trim())
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
      setLabel("")
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
          <Label className="text-sm">Direct Access Tokens</Label>
          <p className="text-xs text-muted-foreground">
            Connect a client without an account — it acts under your name.
          </p>
        </div>
        <Dialog open={createDialogOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm" variant="outline">
              <Plus className="h-4 w-4 mr-1" />
              Generate
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {createdToken ? "Token Created" : "Generate Direct Token"}
              </DialogTitle>
              <DialogDescription>
                {createdToken
                  ? "Copy this token now. You won't be able to see it again."
                  : "Mint a direct access token for this connector."}
              </DialogDescription>
            </DialogHeader>

            {createdToken ? (
              <div className="space-y-2">
                <Label>Access Token</Label>
                <div className="relative">
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
                <p className="text-xs text-amber-600 dark:text-amber-500">
                  Store this token now. It cannot be retrieved again.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="mcp-token-label">Name</Label>
                <Input
                  id="mcp-token-label"
                  placeholder="e.g., External Partner, CI Pipeline"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !createTokenMutation.isPending) {
                      e.preventDefault()
                      handleCreateToken()
                    }
                  }}
                />
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
                  {createTokenMutation.isPending ? "Generating..." : "Generate Token"}
                </Button>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading tokens...</p>
      ) : tokens.length === 0 ? (
        <p className="text-sm text-muted-foreground">No direct tokens yet.</p>
      ) : (
        <div className="space-y-1.5">
          {tokens.map((token: MCPConnectorTokenPublic) => (
            <div
              key={token.id}
              className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                token.revoked ? "opacity-50 bg-muted" : ""
              }`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-medium text-sm truncate">
                  {token.label || "Untitled"}
                </span>
                {token.revoked && (
                  <Badge variant="destructive" className="text-xs shrink-0">
                    Revoked
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <span className="font-mono text-xs text-muted-foreground">
                  {token.prefix}...
                </span>
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
                        revoked: !token.revoked,
                      })
                    }
                    disabled={revokeTokenMutation.isPending}
                  >
                    {token.revoked ? "Restore" : "Revoke"}
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
                        <AlertDialogTitle>Delete Direct Token</AlertDialogTitle>
                        <AlertDialogDescription>
                          Are you sure you want to delete "{token.label || "this token"}"?
                          This action cannot be undone. Any client using it will lose
                          access immediately.
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
