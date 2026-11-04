import { LayoutGrid, X } from "lucide-react"

import { VIEW_GLYPHS } from "@/components/Dashboard/AgentComponents/ComponentPicker"
import type { TileContext } from "@/components/Dashboard/AgentComponents/dashboardFocus"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { type FocusedView, paramPairs } from "./dashboardContext"

const QUOTE_PREVIEW_CHARS = 280

/**
 * The focused tile's context, on the composer's top edge (UI spec S5):
 * "<title> · <params> · 3 selected · quoted text". The tooltip carries what the
 * line has no room for — every param and the quote. `X` leaves it out of the
 * next message.
 */
export function ContextChip({
  tile,
  view,
  onRemove,
}: {
  tile: TileContext
  view: FocusedView
  onRemove: () => void
}) {
  const Glyph = tile.view ? VIEW_GLYPHS[tile.view].icon : LayoutGrid
  const pairs = paramPairs(tile.params)
  const selected = view.selection.length
  const parts = [
    tile.title,
    pairs.length > 0 ? pairs.slice(0, 2).join(", ") : null,
    selected > 0 ? `${selected} selected` : null,
    view.selectedText ? "quoted text" : null,
  ].filter(Boolean)
  const quote =
    view.selectedText && view.selectedText.length > QUOTE_PREVIEW_CHARS
      ? `${view.selectedText.slice(0, QUOTE_PREVIEW_CHARS)}…`
      : view.selectedText
  const hasDetail = pairs.length > 2 || !!quote

  const label = (
    <span className="flex min-w-0 items-center gap-1">
      <Glyph className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate">{parts.join(" · ")}</span>
    </span>
  )

  return (
    <div className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border bg-muted/50 px-2 text-xs">
      {hasDetail ? (
        <Tooltip>
          <TooltipTrigger asChild>
            {/* A button only so a keyboard user can reach the tooltip that carries the chip's full text. */}
            <button
              type="button"
              className="flex min-w-0 cursor-default outline-none focus-visible:underline"
            >
              {label}
            </button>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {pairs.length > 2 && (
              <p className="break-words">{pairs.join(", ")}</p>
            )}
            {quote && (
              <p className="mt-1 line-clamp-6 whitespace-pre-wrap break-words italic">
                {quote}
              </p>
            )}
          </TooltipContent>
        </Tooltip>
      ) : (
        label
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={onRemove}
            data-chip-remove=""
            aria-label="Don't send this tile's context"
            className="flex h-4 w-4 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X className="h-3 w-3" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Don't send this tile's context
        </TooltipContent>
      </Tooltip>
    </div>
  )
}
