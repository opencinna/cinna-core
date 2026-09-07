import { useMutation, useQuery } from "@tanstack/react-query"
import {
  Check,
  ChevronDown,
  Copy,
  Key,
  MonitorDot,
  RefreshCw,
} from "lucide-react"
import { useEffect, useState } from "react"

import type { CLIAccountTokenPublic, CLISetupTokenCreated } from "@/client"
import { CliService } from "@/client"
import { CopyPromptSnippet } from "@/components/Common/CopyPromptSnippet"
import { PreviewList } from "@/components/Common/PreviewList"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import useCustomToast from "@/hooks/useCustomToast"
import { useLocalAgentKitAvailable } from "@/hooks/useLocalAgentKit"
import useRole from "@/hooks/useRole"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { AllCliSessionsSheet } from "./AllCliSessionsSheet"
import { CliSessionRow } from "./CliSessionRow"

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
  const [setupToken, setSetupToken] = useState<CLISetupTokenCreated | null>(
    null,
  )
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [scratchOpen, setScratchOpen] = useState(false)
  const [isSheetOpen, setIsSheetOpen] = useState(false)
  // Hidden entirely when the instance does not publish the starter surface —
  // the prompt would send the user's assistant at a URL that 404s.
  const localAgentKitAvailable = useLocalAgentKitAvailable()

  const { showSuccessToast, showErrorToast } = useCustomToast()

  const {
    data: tokensData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
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
            <CardTitle className="flex items-center gap-2 min-w-0">
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
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="icon"
                      className="rounded-r-none border-r-0"
                      onClick={handleSetup}
                      disabled={createSetupTokenMutation.isPending}
                      aria-label="Regenerate the setup command"
                    >
                      <RefreshCw
                        className={`h-4 w-4 ${createSetupTokenMutation.isPending ? "animate-spin" : ""}`}
                      />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="text-xs">
                    Regenerate
                  </TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="icon"
                      className="rounded-none border-r-0"
                      onClick={() => handleCopy(setupToken.token, "token")}
                      aria-label="Copy the setup token"
                    >
                      {copiedId === "token" ? (
                        <Check className="h-4 w-4 text-success" />
                      ) : (
                        <Key className="h-4 w-4" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="text-xs">
                    {copiedId === "token" ? "Copied" : "Copy token"}
                  </TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="icon"
                      className="rounded-l-none"
                      onClick={() =>
                        handleCopy(setupToken.setup_command, "cmd")
                      }
                      aria-label="Copy the setup command"
                    >
                      {copiedId === "cmd" ? (
                        <Check className="h-4 w-4 text-success" />
                      ) : (
                        <Copy className="h-4 w-4" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="text-xs">
                    {copiedId === "cmd" ? "Copied" : "Copy command"}
                  </TooltipContent>
                </Tooltip>
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
          <p className="mb-2 text-sm font-medium">Active account sessions</p>
          {/* A P5 preview, like App Sessions on this same tab: the card's
              height used to *be* the session count. */}
          <PreviewList
            items={tokens}
            getKey={(token) => token.id}
            renderItem={(token) => <CliSessionRow token={token} />}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load your account sessions"
            empty={
              <p className="text-sm text-muted-foreground">
                {setupToken
                  ? "No machines connected yet — run the setup command above to bootstrap your account workspace."
                  : "No machines connected yet. Use Setup above to generate a bootstrap command."}
              </p>
            }
            onShowAll={() => setIsSheetOpen(true)}
          />
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

      <AllCliSessionsSheet
        tokens={tokens}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />
    </Card>
  )
}
