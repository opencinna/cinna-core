import { Link } from "@tanstack/react-router"
import { Bot, Share2, AlertCircle, Wrench, MessageCircle } from "lucide-react"

import type { AgentPublic } from "@/client"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentCardProps {
  agent: AgentPublic
}

export function AgentCard({ agent }: AgentCardProps) {
  const colorPreset = getColorPreset(agent.ui_color_preset)

  return (
    <Link
      to="/agent/$agentId"
      params={{ agentId: agent.id }}
      className="block h-full"
    >
      <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5 cursor-pointer h-full flex flex-col gap-0">
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
                {/* Clone and update indicators */}
                <div className="flex gap-1 shrink-0">
                  {agent.is_clone && (
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

              {/* Clone mode badge */}
              {agent.is_clone && agent.clone_mode && (
                <Badge
                  variant="secondary"
                  className={`text-xs mt-1.5 ${
                    agent.clone_mode === "builder"
                      ? "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200"
                      : "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200"
                  }`}
                >
                  {agent.clone_mode === "builder" ? (
                    <>
                      <Wrench className="h-3 w-3 mr-1" />
                      Builder Access
                    </>
                  ) : (
                    <>
                      <MessageCircle className="h-3 w-3 mr-1" />
                      Conversation Access
                    </>
                  )}
                </Badge>
              )}
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
      </Card>
    </Link>
  )
}
