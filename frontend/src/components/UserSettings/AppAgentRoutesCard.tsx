/**
 * AppAgentRoutesCard — Settings → Channels, the "MCP Server" card.
 *
 * WHAT IS LEFT OF IT, AND WHY IT IS STILL HERE
 * --------------------------------------------
 * This card used to author App MCP *routes*: a table of agents, per-user
 * assignments, and toggles. That table is gone — App MCP now routes over
 * exactly the agents the caller owns (any with a `router_trigger_prompt`)
 * plus the identities they have enabled, the same candidate set every
 * server channel uses. There is nothing left to author.
 *
 * What survives is the one thing this card was also the only home for: the
 * **MCP Server URL** and the connect walkthrough behind the help button.
 * `UserChannelsCard` above renders the App MCP channel row — its switch and
 * its agent scope — but it renders no transport-specific detail for any
 * channel, so it never shows the endpoint or the setup steps. Folding this
 * card away without folding the URL somewhere else would leave a user able
 * to switch App MCP on and unable to find the address to point a client at.
 *
 * WHAT THIS CARD DELIBERATELY NO LONGER SHOWS
 * -------------------------------------------
 * Per-person identity toggles. They live on `UserChannelsCard`, on this same
 * tab, written through `IdentityContactsService`. This card used to render a
 * second copy of them over a raw `fetch` and without the consent copy that
 * explains which way the switch points — two controls for one fact, stacked
 * on one screen, one of them silent about what it consents to.
 */
import { useQuery } from "@tanstack/react-query"
import { Check, Copy, HelpCircle, Network } from "lucide-react"
import { useState } from "react"

import { UtilsService } from "@/client"
import { GettingStartedModal } from "@/components/Onboarding/GettingStartedModal"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

export function AppAgentRoutesCard() {
  const [copied, setCopied] = useState(false)
  const [showHelp, setShowHelp] = useState(false)

  const { data: mcpInfo } = useQuery({
    queryKey: ["mcp-info"],
    queryFn: () => UtilsService.getMcpInfo(),
    staleTime: Infinity,
  })

  const appMcpUrl = mcpInfo?.mcp_server_url ?? ""

  const handleCopyUrl = () => {
    navigator.clipboard.writeText(appMcpUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Network className="h-4 w-4 text-blue-500" />
            MCP Server
          </CardTitle>
        </div>
        <CardDescription>
          Connect external MCP clients to the Application MCP Server
        </CardDescription>
        <div className="flex items-center gap-2 p-2 bg-muted rounded-md">
          {appMcpUrl ? (
            <>
              <code className="flex-1 text-xs truncate">{appMcpUrl}</code>
              <Button
                size="icon"
                variant="ghost"
                className="h-6 w-6 shrink-0"
                onClick={() => setShowHelp(true)}
              >
                <HelpCircle className="h-3 w-3" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                className="h-6 w-6 shrink-0"
                onClick={handleCopyUrl}
              >
                {copied ? (
                  <Check className="h-3 w-3" />
                ) : (
                  <Copy className="h-3 w-3" />
                )}
              </Button>
            </>
          ) : (
            <span className="text-xs text-muted-foreground italic">
              MCP Server URL not configured
            </span>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Point Claude Desktop, Cursor, or any MCP client at this URL and
          authorise it. The client gets a single{" "}
          <code className="text-[11px] bg-muted px-1 py-0.5 rounded">
            send_message
          </code>{" "}
          tool that reaches every agent you own with a Trigger Prompt, plus the
          identities you have enabled.
        </p>
        <p className="text-xs text-muted-foreground">
          Whether this channel is on for you, and which of your agents it may
          reach, are set on the{" "}
          <span className="font-medium text-foreground">App MCP Server</span>{" "}
          row above.
        </p>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setShowHelp(true)}
          className="w-full"
        >
          <HelpCircle className="h-3.5 w-3.5 mr-2" />
          Connect instructions
        </Button>
      </CardContent>

      <GettingStartedModal
        open={showHelp}
        onOpenChange={setShowHelp}
        initialArticle="app-mcp-setup"
      />
    </Card>
  )
}
