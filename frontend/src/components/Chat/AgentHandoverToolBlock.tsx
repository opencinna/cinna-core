import { useQuery } from "@tanstack/react-query"
import { ArrowRight, Bot } from "lucide-react"

import { AgentsService } from "@/client"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentHandoverToolBlockProps {
  targetAgentId: string
  targetAgentName: string
  handoverMessage: string
}

export function AgentHandoverToolBlock({
  targetAgentId,
  targetAgentName,
  handoverMessage,
}: AgentHandoverToolBlockProps) {
  // Fetch agent data to get color preset
  const { data: agent } = useQuery({
    queryKey: ["agent", targetAgentId],
    queryFn: () => AgentsService.readAgent({ agentId: targetAgentId }),
    enabled: !!targetAgentId,
  })

  const colorPreset = getColorPreset(agent?.ui_color_preset)

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <ArrowRight className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-foreground/90">Hand over conversation to</span>
          <div className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md ${colorPreset.badgeBg}`}>
            <Bot className={`h-3.5 w-3.5 ${colorPreset.iconText}`} />
            <span className={`font-medium text-xs ${colorPreset.badgeText}`}>
              {targetAgentName}
            </span>
          </div>
        </div>
        {handoverMessage && (
          <div className="mt-2 pl-3 border-l-2 border-muted">
            <div className="text-xs text-foreground/70 whitespace-pre-wrap break-words">
              {handoverMessage}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
