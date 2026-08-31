/**
 * McpConnectorsCardSimple — degraded view of the MCP Connectors card for
 * the ``agent-user`` role.
 *
 * App MCP exposure is automatic: every agent the user owns is a routing
 * candidate as soon as it has a ``router_trigger_prompt``. There is no
 * route to create and no per-user toggle to flip, so this card is purely
 * explanatory. Hides every developer-tier affordance:
 *
 *   - No "New" / "Add" dialog (no creating direct connectors or identity
 *     bindings)
 *   - No user-share multi-select
 *   - Trigger prompt is a read-only mirror of the agent's
 *     ``router_trigger_prompt`` (editable via the Configuration tab)
 *
 * The copy carries the whole feature explanation, because this card is an
 * ``agent-user``'s only exposure to App MCP routing — they never see the
 * developer card's creation dialog, so nothing else tells them how the
 * agent gets picked. Two things have to be legible without docs:
 *
 *   1. That the agent is reachable from external MCP clients, and what
 *      that does NOT affect (chat here, schedules, email, webhooks),
 *   2. What the trigger prompt is for (the router picks between the
 *      user's agents with it), and where to edit it.
 */
import { Link } from "@tanstack/react-router"
import { Unplug } from "lucide-react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"

interface McpConnectorsCardSimpleProps {
  routerTriggerPrompt?: string | null
}

export function McpConnectorsCardSimple({
  routerTriggerPrompt,
}: McpConnectorsCardSimpleProps) {
  const triggerPrompt = routerTriggerPrompt?.trim() || ""

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Unplug className="h-5 w-5" />
          MCP Connectors
        </CardTitle>
        <CardDescription>
          Use this agent from apps outside Cinna — Claude Desktop, Cursor, or
          any MCP client connected to your personal MCP Server URL.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!triggerPrompt ? (
          <div className="rounded-md border border-dashed p-4 space-y-2 text-sm">
            <p className="font-medium">Not available in MCP clients yet</p>
            <p className="text-xs text-muted-foreground">
              This agent needs a <span className="font-medium">Trigger
              Prompt</span> — one sentence describing when it should be used —
              before an MCP client can route anything to it. Set one on the{" "}
              <span className="font-medium">Configuration</span> tab and it
              becomes reachable right away.
            </p>
          </div>
        ) : (
          <div className="rounded-md border divide-y">
            {/* ── What "reachable" means here ───────────────────────── */}
            <div className="p-4 space-y-1">
              <p className="font-medium text-sm">
                Available in external MCP clients
              </p>
              <p className="text-xs text-muted-foreground">
                Your MCP client can send messages to this agent. Every agent
                you own is reachable this way once it has a trigger prompt —
                there is nothing to switch on.
              </p>
            </div>

            {/* ── The trigger prompt, explained ─────────────────────── */}
            <div className="p-4 space-y-2">
              <p className="text-sm font-medium">When this agent gets picked</p>
              <p className="text-xs text-muted-foreground">
                One MCP Server URL reaches all of your agents. This sentence is
                how it decides that a message belongs to this one:
              </p>
              <blockquote className="border-l-2 pl-3 text-xs italic text-muted-foreground line-clamp-4">
                {triggerPrompt}
              </blockquote>
              <p className="text-xs text-muted-foreground">
                This sentence is the agent's{" "}
                <span className="font-medium text-foreground">
                  Trigger Prompt
                </span>{" "}
                — edit it on the{" "}
                <span className="font-medium">Configuration</span> tab.
              </p>
            </div>
          </div>
        )}

        <Separator className="my-4" />

        <p className="text-xs text-muted-foreground">
          MCP reachability is separate from everything else — chat here,
          schedules, and other integrations are unaffected. Your MCP Server URL
          and setup steps live in{" "}
          <Link
            to="/settings"
            hash="channels"
            className="font-medium underline underline-offset-2 hover:text-foreground"
          >
            Settings → Channels
          </Link>
          .
        </p>
      </CardContent>
    </Card>
  )
}
