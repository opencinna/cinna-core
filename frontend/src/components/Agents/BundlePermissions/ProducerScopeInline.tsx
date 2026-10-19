import type { BundlePermissionProducer } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

interface ProducerScopeInlineProps {
  producer: BundlePermissionProducer
  /** This row's scope grant on the producer; `undefined` = none yet. */
  grant: { scopes?: string[] } | undefined
  onEdit: () => void
}

/**
 * Inline scope-chip cluster for one manageable producer on one person's or
 * group's row. Clicking opens the edit modal for that row. Never rendered for
 * non-manageable producers (their grants are unavailable).
 */
export function ProducerScopeInline({
  producer,
  grant,
  onEdit,
}: ProducerScopeInlineProps) {
  const scopes = grant?.scopes ?? []

  // The count, not the list: a row carrying three producers' worth of scope
  // chips wraps to three lines and sets the height of the whole list. The
  // names live in the tooltip and the editor.
  const summary = !grant
    ? "assign"
    : scopes.length === 0
      ? "none"
      : `${scopes.length}`

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onEdit}
          className="flex h-7 min-w-0 max-w-[160px] items-center gap-1.5 overflow-hidden rounded-md border px-1.5 text-left whitespace-nowrap transition-colors hover:bg-accent"
        >
          <AgentBadge
            agent={{
              id: producer.producer_agent_id,
              name: producer.producer_agent_name || "Producer",
              ui_color_preset: producer.producer_ui_color_preset,
            }}
            linkTo="none"
            // The chip is width-capped, so the producer's name truncates
            // rather than wrapping the whole cluster to a second line.
            className="min-w-0 max-w-[110px] [&>svg]:shrink-0"
          />
          <span
            className={cn(
              "shrink-0 text-[11px]",
              grant && scopes.length > 0
                ? "font-medium"
                : "text-muted-foreground",
            )}
          >
            {summary}
          </span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        <p className="font-medium">
          {producer.producer_agent_name || "Producer"}
        </p>
        <p>
          {!grant
            ? "No scopes assigned yet — click to assign."
            : scopes.length === 0
              ? "Granted access, but no scopes. Click to edit."
              : scopes.join(", ")}
        </p>
      </TooltipContent>
    </Tooltip>
  )
}
