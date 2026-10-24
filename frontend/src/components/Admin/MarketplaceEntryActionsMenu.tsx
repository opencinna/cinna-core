import { Ban, Check, Eye, Info, ListX, Loader2 } from "lucide-react"

import type { LLMPluginMarketplacePluginPublic } from "@/client"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useEntryPending } from "./marketplaceEntryAccess"

type Entry = LLMPluginMarketplacePluginPublic

export interface MarketplaceEntryActions {
  onAllow: (entry: Entry) => void
  /** Opens the list-owned Ban dialog (the menu item unmounts on select). */
  onBan: (entry: Entry) => void
  /** Back to `default` — Unban for a banned entry, Remove for an allowed one. */
  onReset: (entry: Entry) => void
  onOpen: (entry: Entry) => void
}

/**
 * The entry row's `⋯` (UI spec S1, K5): Allow · Ban… · Unban / Remove from
 * allowlist, then Open details. Ban carries no destructive styling: it is
 * reversible, and its dialog is a reason form. While a write on the row is in
 * flight the slot shows a spinner naming it.
 */
export function MarketplaceEntryActionsMenu({
  entry,
  actions,
}: {
  entry: Entry
  actions: MarketplaceEntryActions
}) {
  const pending = useEntryPending(entry)

  if (pending) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex h-7 w-7 items-center justify-center"
            aria-live="polite"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="sr-only">{pending}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          {pending}
        </TooltipContent>
      </Tooltip>
    )
  }

  const allowed = entry.access === "allowed"
  const banned = entry.access === "banned"

  return (
    <RowActionsMenu label={`the plugin ${entry.name}`}>
      {/* A banned row is lifted with Unban, not Allow: one way back. */}
      {!allowed && !banned && (
        <DropdownMenuItem onSelect={() => actions.onAllow(entry)}>
          <Check />
          Allow
        </DropdownMenuItem>
      )}
      {!banned && (
        <DropdownMenuItem onSelect={() => actions.onBan(entry)}>
          <Ban />
          Ban…
        </DropdownMenuItem>
      )}
      {banned && (
        <DropdownMenuItem onSelect={() => actions.onReset(entry)}>
          <Eye />
          Unban
        </DropdownMenuItem>
      )}
      {allowed && (
        <DropdownMenuItem onSelect={() => actions.onReset(entry)}>
          <ListX />
          Remove from allowlist
        </DropdownMenuItem>
      )}
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={() => actions.onOpen(entry)}>
        <Info />
        Open details
      </DropdownMenuItem>
    </RowActionsMenu>
  )
}
