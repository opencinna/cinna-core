import { Link } from "@tanstack/react-router"
import { Bot, Share2, AlertCircle } from "lucide-react"

import type { AgentPublic, AgentStatusPublic } from "@/client"
import { cn } from "@/lib/utils"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getColorPreset } from "@/utils/colorPresets"
import { AgentStatusCardFooter } from "./AgentStatusCardFooter"

interface AgentCardProps {
  agent: AgentPublic
  status?: AgentStatusPublic | null
}

export function AgentCard({ agent, status }: AgentCardProps) {
  const colorPreset = getColorPreset(agent.ui_color_preset)
  const hasStatusFooter =
    !!status && (status.severity != null || status.raw != null)

  return (
    <Card
      className={cn(
        "relative transition-all hover:shadow-md hover:-translate-y-0.5 h-full flex flex-col gap-0 overflow-hidden",
        hasStatusFooter && "pb-0",
      )}
    >
      <Link
        to="/agent/$agentId"
        params={{ agentId: agent.id }}
        className="flex-1 flex flex-col cursor-pointer"
      >
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div className={`rounded-lg p-2 ${colorPreset.iconBg}`}>
              <Bot className={`h-5 w-5 ${colorPreset.iconText}`} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-start justify-between gap-1">
                <CardTitle className="text-lg break-words">
                  {agent.name}
                </CardTitle>
                {/* Bundle and update indicators */}
                <div className="flex gap-1 shrink-0">
                  {agent.bundle_uuid && !agent.is_publisher_install && (
                    <Share2 className="h-3.5 w-3.5 text-muted-foreground" />
                  )}
                  {agent.pending_update && (
                    <Badge variant="destructive" className="text-xs px-1.5 py-0">
                      <AlertCircle className="h-3 w-3 mr-0.5" />
                      Update
                    </Badge>
                  )}
                </div>
              </div>
            </div>
          </div>
        </CardHeader>
        {agent.entrypoint_prompt && (
          <CardContent className="pt-0 flex-1 min-h-0">
            <pre className="text-xs bg-muted/50 rounded-md p-3 overflow-hidden whitespace-pre-wrap break-words font-mono line-clamp-4">
              {agent.entrypoint_prompt}
            </pre>
          </CardContent>
        )}
      </Link>
      {status && <AgentStatusCardFooter agentId={agent.id} status={status} />}
    </Card>
  )
}
