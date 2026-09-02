import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronDown, Copy, Check, Key, Laptop, MonitorDot, RefreshCw, Unplug } from "lucide-react"
import { useState, useEffect } from "react"

import type { CLISetupTokenCreated, CLIAccountTokenPublic } from "@/client"
import { CliService } from "@/client"
import { CopyPromptSnippet } from "@/components/Common/CopyPromptSnippet"
import useCustomToast from "@/hooks/useCustomToast"
import { useLocalAgentKitAvailable } from "@/hooks/useLocalAgentKit"
import useRole from "@/hooks/useRole"
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

function formatCountdown(seconds: number): string {
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60)
    const s = seconds % 60
    return s > 0 ? `${m}m ${s}s` : `${m}m`
  }
  return `${seconds}s`
}

/**
 * Account-level Local Development card. Bootstraps a single account workspace
 * from which a local coding agent can discover, sync, and exec into any agent
 * the user has building rights on. Developer-gated (an agent-user can't even
 * generate the link).
 */
export function LocalDevelopmentCard() {
  const { isDeveloper } = useRole()
  const [setupToken, setSetupToken] = useState<CLISetupTokenCreated | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [scratchOpen, setScratchOpen] = useState(false)
  // Hidden entirely when the instance does not publish the starter surface —
  // the prompt would send the user's assistant at a URL that 404s.
  const localAgentKitAvailable = useLocalAgentKitAvailable()

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: tokensData, isLoading } = useQuery({
    queryKey: ["account-cli-tokens"],
    queryFn: () => CliService.listAccountTokens(),
    enabled: isDeveloper,
  })

  const tokens: CLIAccountTokenPublic[] = tokensData?.data ?? []

  const createSetupTokenMutation = useMutation({
    mutationFn: () => CliService.createAccountSetupToken(),
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

  const revokeAccountTokenMutation = useMutation({
    mutationFn: (tokenId: string) =>
      CliService.revokeAccountToken({ tokenId }),
    onSuccess: () => {
      showSuccessToast("Account session disconnected")
      queryClient.invalidateQueries({ queryKey: ["account-cli-tokens"] })
    },
    onError: () => {
      showErrorToast("Failed to disconnect account session")
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

  if (!isDeveloper) {
    return null
  }

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
              Drive your whole agent network from your local coding assistant —
              one bootstrap, then sync and exec into any agent you can build.
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
          <p className="text-sm font-medium mb-2">Active Account Sessions</p>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : tokens.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {setupToken
                ? "No active account sessions yet. Run the setup command above to bootstrap your account workspace."
                : "No active account sessions. Click Set up Local Development to generate a bootstrap command."}
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {tokens.map((token) => (
                <li
                  key={token.id}
                  className="flex items-center justify-between gap-3 py-3"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <Laptop className="h-4 w-4 text-muted-foreground" />
                    <div className="min-w-0">
                      <p className="text-sm font-medium truncate">
                        {token.name || token.prefix}
                      </p>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-muted-foreground text-xs">
                          {token.child_count} agent
                          {token.child_count === 1 ? "" : "s"} synced
                        </span>
                      </div>
                    </div>
                  </div>

                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="shrink-0 text-muted-foreground hover:text-destructive"
                        title="Disconnect"
                        aria-label="Disconnect account session"
                      >
                        <Unplug className="h-4 w-4" />
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent
                      onOpenAutoFocus={(e) => e.preventDefault()}
                    >
                      <AlertDialogHeader>
                        <AlertDialogTitle>
                          Disconnect Account Session
                        </AlertDialogTitle>
                        <AlertDialogDescription>
                          Revoking disconnects all agents synced from this
                          machine ({token.child_count} agent
                          {token.child_count === 1 ? "" : "s"}). Local files
                          remain intact, but the CLI will need to be set up
                          again.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          autoFocus
                          onClick={() =>
                            revokeAccountTokenMutation.mutate(token.id)
                          }
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                        >
                          Disconnect
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/*
          The setup command above bootstraps a *cloud* workspace and needs this
          account. Someone on a machine with nothing on it yet needs the other
          entrypoint — the public starter kit, which asks for no account at all —
          so it is offered here, collapsed, rather than competing with Setup.
        */}
        {localAgentKitAvailable && (
        <div className="mt-4 border-t pt-3">
          <button
            type="button"
            onClick={() => setScratchOpen((open) => !open)}
            className="flex w-full items-center gap-1.5 text-left text-xs text-muted-foreground hover:text-foreground"
            aria-expanded={scratchOpen}
          >
            <ChevronDown
              className={`h-3.5 w-3.5 transition-transform ${scratchOpen ? "" : "-rotate-90"}`}
            />
            Starting from scratch on a new machine? Paste into your coding
            assistant
          </button>
          {scratchOpen && <CopyPromptSnippet className="mt-2" />}
        </div>
        )}
      </CardContent>
    </Card>
  )
}
