import { Link } from "@tanstack/react-router"
import { Bot } from "lucide-react"

import type { AgentPublic } from "@/client"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
              <CardTitle className="text-lg break-words">
                {agent.name}
              </CardTitle>
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
