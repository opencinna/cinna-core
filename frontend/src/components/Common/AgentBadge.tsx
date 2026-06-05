import { Link } from "@tanstack/react-router"
import { Bot } from "lucide-react"

import { cn } from "@/lib/utils"
import { getColorPreset } from "@/utils/colorPresets"

/**
 * Canonical colored "agent pill" — a lucide ``Bot`` glyph + the agent name,
 * tinted by the agent's ``ui_color_preset``. Replaces the inline
 * ``getColorPreset(...).badgeBg/badgeText`` markup that was hand-rolled across
 * the credentials / agent-api connection maps.
 *
 * Pass ``linkTo="agent"`` to make the pill navigate to the agent detail page
 * (``/agent/$agentId``) with a subtle hover affordance; omit it (or pass
 * ``"none"``) for a static pill. The visual style is identical in both modes.
 */
type AgentLike = {
  id: string
  name: string
  ui_color_preset?: string | null
}

interface AgentBadgeProps {
  agent: AgentLike
  /** ``"agent"`` links to the agent detail page; ``"none"`` renders a span. */
  linkTo?: "agent" | "none"
  /**
   * ``"sm"`` (default) — the compact ``rounded-full`` connection-map pill.
   * ``"md"`` — the larger ``rounded-md`` chip used in impact/warning lists.
   */
  size?: "sm" | "md"
  className?: string
}

export function AgentBadge({
  agent,
  linkTo = "none",
  size = "sm",
  className,
}: AgentBadgeProps) {
  const preset = getColorPreset(agent.ui_color_preset)

  const base = cn(
    "inline-flex items-center font-medium",
    size === "sm"
      ? "gap-1 rounded-full px-2 py-0.5 text-xs"
      : "gap-2 rounded-md px-3 py-1.5 text-sm",
    preset.badgeBg,
    preset.badgeText,
    className,
  )

  const iconSize = size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5"

  const content = (
    <>
      <Bot className={iconSize} />
      {agent.name}
    </>
  )

  if (linkTo === "agent") {
    return (
      <Link
        to="/agent/$agentId"
        params={{ agentId: agent.id }}
        className={cn(base, "transition-opacity hover:opacity-80")}
      >
        {content}
      </Link>
    )
  }

  return <span className={base}>{content}</span>
}
