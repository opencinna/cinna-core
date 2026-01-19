import { useQuery } from "@tanstack/react-query"
import { ArrowRight, Bot, Inbox } from "lucide-react"

import { AgentsService } from "@/client"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentHandoverToolBlockProps {
  targetAgentId?: string
  targetAgentName?: string
  taskMessage: string
}

export function AgentHandoverToolBlock({
  targetAgentId,
  targetAgentName,
  taskMessage,
}: AgentHandoverToolBlockProps) {
  // Determine if this is a direct handover or inbox task
  const isDirectHandover = !!targetAgentId && !!targetAgentName

  // Fetch agent data to get color preset (only for direct handover)
  const { data: agent } = useQuery({
    queryKey: ["agent", targetAgentId],
    queryFn: () => AgentsService.readAgent({ id: targetAgentId! }),
    enabled: isDirectHandover,
  })

  const colorPreset = getColorPreset(agent?.ui_color_preset)

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      {isDirectHandover ? (
        <ArrowRight className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      ) : (
        <Inbox className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      )}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          {isDirectHandover ? (
            <>
              <span className="text-foreground/90">Hand over task to</span>
              <div className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md ${colorPreset.badgeBg}`}>
                <Bot className={`h-3.5 w-3.5 ${colorPreset.iconText}`} />
                <span className={`font-medium text-xs ${colorPreset.badgeText}`}>
                  {targetAgentName}
                </span>
              </div>
            </>
          ) : (
            <span className="text-foreground/90">Creating task in user's inbox</span>
          )}
        </div>
        {taskMessage && (
          <div className="mt-2 pl-3 border-l-2 border-muted">
            <div className="text-xs text-foreground/70 whitespace-pre-wrap break-words">
              {taskMessage}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// Backward compatibility alias
export { AgentHandoverToolBlock as CreateAgentTaskToolBlock }
