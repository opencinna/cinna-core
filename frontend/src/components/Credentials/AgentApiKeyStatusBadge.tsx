import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  agentApiKeyStatusLabel,
  isPausedAgentApiKeyStatus,
} from "./agentApiKeyCopy"

interface AgentApiKeyStatusBadgeProps {
  /** ``AgentApiKeyStatus`` — or any string the server sends; unknown = ended. */
  status: string | null | undefined
  /** One-sentence explanation, shown as the badge's tooltip when given. */
  sentence?: string
  className?: string
}

/**
 * The one badge an issued key's status gets, wherever a badge is used (the
 * holder's detail card, the credentials grid, the agent's Credentials tab), so
 * the same state reads the same in every list (§2 "The same entity, before and
 * after").
 *
 * Renders **nothing** for ``active`` or a missing status: the absence of a
 * problem is not a chip (§2 Absent facts). Paused/disabled is the solid warning
 * pair; every ended or unknown value is the shared destructive variant. Solid
 * colour with its ``-foreground`` partner, never tinted text on its own tint —
 * that measures 2–3:1 (R14a). ``h-5`` so a badged line is not taller.
 */
export function AgentApiKeyStatusBadge({
  status,
  sentence,
  className,
}: AgentApiKeyStatusBadgeProps) {
  if (!status || status === "active") return null

  const badge = isPausedAgentApiKeyStatus(status) ? (
    <Badge
      className={cn(
        "h-5 border-transparent bg-warning text-warning-foreground",
        className,
      )}
    >
      {agentApiKeyStatusLabel(status)}
    </Badge>
  ) : (
    <Badge variant="destructive" className={cn("h-5", className)}>
      {agentApiKeyStatusLabel(status)}
    </Badge>
  )

  if (!sentence) return badge

  return (
    <Tooltip>
      <TooltipTrigger asChild>{badge}</TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        {sentence}
      </TooltipContent>
    </Tooltip>
  )
}
