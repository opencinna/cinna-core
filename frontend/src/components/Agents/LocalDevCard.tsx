import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Copy, Check, Key, MonitorDot, RefreshCw, Unplug } from "lucide-react"
import { useState, useEffect } from "react"

import type { CLISetupTokenCreated, CLITokenPublic } from "@/client"
import { CliService } from "@/client"
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
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { LocalDevSyncStatus } from "@/components/Agents/LocalDevSyncStatus"

function formatCountdown(seconds: number): string {
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60)
    const s = seconds % 60
    return s > 0 ? `${m}m ${s}s` : `${m}m`
  }
  return `${seconds}s`
}

interface LocalDevCardProps {
  agentId: string
}

export function LocalDevCard({ agentId }: LocalDevCardProps) {
  const [setupToken, setSetupToken] = useState<CLISetupTokenCreated | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: tokensData, isLoading } = useQuery({
    queryKey: ["cli-tokens", agentId],
    queryFn: () => CliService.listCliTokens({ agentId }),
  })

  const tokens: CLITokenPublic[] = tokensData?.data ?? []

  const createSetupTokenMutation = useMutation({
    mutationFn: () =>
      CliService.createSetupToken({ requestBody: { agent_id: agentId } }),
    onSuccess: (data) => {
      setSetupToken(data)
      const secs = Math.max(
        0,
        Math.floor((new Date(data.expires_at).getTime() - Date.now()) / 1000),
      )
      setSecondsLeft(secs)
      showSuccessToast("Setup command generated")
    },
    onError: () => {
      showErrorToast("Failed to generate setup command")
    },
  })

  const revokeCliTokenMutation = useMutation({
    mutationFn: (tokenId: string) =>
      CliService.revokeCliToken({ tokenId }),
    onSuccess: () => {
      showSuccessToast("Session disconnected")
      queryClient.invalidateQueries({ queryKey: ["cli-tokens", agentId] })
    },
    onError: () => {
      showErrorToast("Failed to disconnect session")
    },
  })

  // Countdown timer for setup token expiry
  useEffect(() => {
    if (!setupToken) return
    const interval = setInterval(() => {
      const secs = Math.max(
        0,
        Math.floor(
          (new Date(setupToken.expires_at).getTime() - Date.now()) / 1000,
        ),
      )
      setSecondsLeft(secs)
      if (secs <= 0) {
        clearInterval(interval)
      }
    }, 1000)
    return () => clearInterval(interval)
  }, [setupToken])

  const handleSetup = () => {
    createSetupTokenMutation.mutate()
  }

  const handleCopy = async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }


  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <MonitorDot className="h-5 w-5" />
              Local Development
            </CardTitle>
            <CardDescription>
              Develop this agent locally with live file sync to the remote environment
            </CardDescription>
          </div>
          <Button
            size="sm"
            onClick={handleSetup}
            disabled={createSetupTokenMutation.isPending}
          >
            {createSetupTokenMutation.isPending ? "Generating..." : "Setup"}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {setupToken && (
          <div className="space-y-2 mb-4">
            <Label className="text-xs text-muted-foreground">
              Setup Command
            </Label>
            <div className="flex gap-2">
              <Input
                value={setupToken.setup_command}
                readOnly
                className="font-mono text-xs"
              />
              <div className="flex shrink-0">
                <Button
                  variant="outline"
                  size="icon"
                  className="rounded-r-none border-r-0"
                  onClick={handleSetup}
                  disabled={createSetupTokenMutation.isPending}
                  title="Regenerate"
                >
                  <RefreshCw
                    className={`h-4 w-4 ${createSetupTokenMutation.isPending ? "animate-spin" : ""}`}
                  />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  className="rounded-none border-r-0"
                  onClick={() => handleCopy(setupToken.token, "token")}
                  title="Copy token"
                >
                  {copiedId === "token" ? (
                    <Check className="h-4 w-4 text-green-500" />
                  ) : (
                    <Key className="h-4 w-4" />
                  )}
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  className="rounded-l-none"
                  onClick={() => handleCopy(setupToken.setup_command, "cmd")}
                  title="Copy command"
                >
                  {copiedId === "cmd" ? (
                    <Check className="h-4 w-4 text-green-500" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                </Button>
              </div>
            </div>
            {secondsLeft > 0 && (
              <p className="text-xs text-muted-foreground">
                Expires in {formatCountdown(secondsLeft)}
              </p>
            )}
          </div>
        )}

        <div>
          <p className="text-sm font-medium mb-2">Active Sessions</p>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : tokens.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {setupToken
                ? "No active sessions yet. Run the setup command above to get started. Files sync live with the remote environment."
                : "No active sessions. Click Setup to generate an install command."}
            </p>
          ) : (
            <div className="space-y-1.5">
              {tokens.map((token) => (
                <div
                  key={token.id}
                  className="flex items-center justify-between px-3 py-2 border rounded-lg"
                >
                  <div className="min-w-0 flex-1">
                    <p className="font-medium text-sm truncate">
                      {token.name || token.prefix}
                    </p>
                    <div className="flex items-center gap-2 mt-0.5">
                      <LocalDevSyncStatus lastSyncConnectedAt={token.last_sync_connected_at} />
                    </div>
                  </div>
                  <div className="shrink-0 ml-2">
                    <AlertDialog>
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <AlertDialogTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label="Disconnect session"
                                className="h-7 w-7 text-destructive hover:text-destructive"
                              >
                                <Unplug className="h-4 w-4" />
                              </Button>
                            </AlertDialogTrigger>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            Disconnect
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                      <AlertDialogContent
                        onOpenAutoFocus={(e) => e.preventDefault()}
                      >
                        <AlertDialogHeader>
                          <AlertDialogTitle>Disconnect Session</AlertDialogTitle>
                          <AlertDialogDescription>
                            This will revoke the CLI token. The local files
                            remain intact, but the CLI will need to be set up
                            again.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction
                            autoFocus
                            onClick={() =>
                              revokeCliTokenMutation.mutate(token.id)
                            }
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          >
                            Disconnect
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
