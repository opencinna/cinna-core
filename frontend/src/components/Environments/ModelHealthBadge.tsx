import { AlertTriangle } from "lucide-react"
import type { ModelHealthPublic } from "@/client"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/**
 * Amber/orange badge surfaced when an environment is configured to use a
 * deprecated, retired, or unavailable AI model. Distinct from the admin image
 * tag "Stale" badge — remediation here is reconfigure/restart (or editing the
 * model override), NOT a Docker rebuild.
 *
 * The tooltip lists the flagged per-mode model + plain-language reason. The
 * optional `onAction` wires the cause CTA (e.g. open the override editor or
 * restart); when omitted the badge is informational only.
 */
export function ModelHealthBadge({
  modelHealth,
  onAction,
}: {
  modelHealth?: ModelHealthPublic | null
  onAction?: () => void
}) {
  if (!modelHealth?.has_warning) return null

  const flagged = (modelHealth.modes ?? []).filter(
    (m) => m.status === "retired_override" || m.status === "unknown_model",
  )
  // Prefer a frozen-override CTA when present (more actionable).
  const primaryCta =
    flagged.find((m) => m.cause === "frozen_override")?.cta ??
    flagged[0]?.cta ??
    "Restart to use the current model."

  const badge = (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200 cursor-default"
      role={onAction ? "button" : undefined}
      onClick={onAction}
    >
      <AlertTriangle className="h-3 w-3" />
      Model
    </span>
  )

  return (
    <Tooltip>
      <TooltipTrigger asChild>{badge}</TooltipTrigger>
      <TooltipContent className="max-w-xs">
        <p className="text-xs font-medium mb-1">AI model needs updating</p>
        {flagged.map((m) => (
          <p key={m.mode} className="text-xs">
            <span className="capitalize">{m.mode}</span>:{" "}
            <span className="font-mono">{m.model}</span>
          </p>
        ))}
        <p className="text-xs text-muted-foreground mt-1">{primaryCta}</p>
      </TooltipContent>
    </Tooltip>
  )
}
