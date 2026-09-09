import { AlertTriangle, BookOpen, GraduationCap, Store } from "lucide-react"
import { useState } from "react"

import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type { AddAddonResult } from "@/utils/addons"
import { AddAddonResultDetailDialog } from "./AddAddonResultDetailDialog"

interface AddAddonResultRowProps {
  result: AddAddonResult
  selected: boolean
  onSelect: () => void
}

/**
 * A search result, as the same house row the installed list uses.
 *
 * Rows rather than tiles, deliberately: what you install must look like what
 * you get, and the grid of `PluginCard`s this replaces read as a different
 * kind of object from the row it turned into.
 *
 * The whole row is the selection control, which is what makes step 2 possible:
 * with a per-row Install button there would be nowhere to choose the modes
 * without a third disclosure level. Its one nested control is the **Details**
 * button — the same glyph the installed row carries, opening the same kind of
 * read-only dialog — and it sits right after the badges rather than in the
 * action cluster: the list scrolls, and a control at the row's far edge lands
 * under the scrollbar. Its click stops at the button so opening Details never
 * also picks the row.
 *
 * Two badges, on purpose (guidelines say one): the version people scan an
 * install list by, and the author — in a list that merges several
 * marketplaces with the catalog, *who made it* is the fact that tells two
 * same-named entries apart.
 *
 * An entry this platform cannot install stays in the list: the install
 * endpoint answers one with `409 plugin_unsupported`, so it is shown with its
 * reason and no way to pick it. Hiding those instead would make an admin's
 * half-broken marketplace look empty (plan §10) — the user would search, find
 * nothing, and have nothing to take to the admin. What the agent already
 * carries is *not* here: `buildAddonResults` drops it, since it is a row on
 * the card behind this dialog already.
 */
export function AddAddonResultRow({
  result,
  selected,
  onSelect,
}: AddAddonResultRowProps) {
  const [detailOpen, setDetailOpen] = useState(false)

  // The glyph follows the *kind*, which the fold derived from the marketplace
  // format — so every row under the Skills filter carries the skill glyph, a
  // skills-format marketplace entry included. Which marketplace or publisher it
  // came from is the `origin` fact in Details; the tile answers "what is this".
  const Icon = result.kind === "skill" ? GraduationCap : Store
  const noun = result.kind === "skill" ? "skill" : "plugin"
  const disabled = !result.supported
  // Why it cannot be picked, in the row's *name*. An explicit `aria-label` on
  // a `role="option"` overrides the computed name, so a reason that lived only
  // in a flag would be unreachable for a reader who lands on a disabled row:
  // they would hear "Plugin Foo, dimmed" and no cause at all.
  const blockedReason = result.unsupportedReason

  return (
    <div
      // `option` inside the list's `listbox`, not `button`: choosing one of n
      // results *is* a single-select listbox, it is the role that announces
      // "3 of 12, selected", and a real `<button>` could not wrap the row —
      // `ListRow` renders `div`s, which are not valid inside one.
      role="option"
      tabIndex={disabled ? -1 : 0}
      aria-selected={selected}
      aria-disabled={disabled || undefined}
      aria-label={[
        result.kind === "skill" ? "Skill" : "Plugin",
        " ",
        result.name,
        blockedReason ? ` — ${blockedReason}` : "",
      ].join("")}
      className={cn(
        "outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        !disabled && "cursor-pointer",
        selected && "bg-primary/10 ring-1 ring-inset ring-primary",
      )}
      onClick={() => {
        if (!disabled) onSelect()
      }}
      onKeyDown={(e) => {
        if (disabled) return
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          onSelect()
        }
      }}
    >
      <ListRow
        muted={disabled}
        icon={
          <span className="flex h-6 w-6 items-center justify-center rounded bg-muted">
            <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          </span>
        }
        title={result.name}
        badges={
          <>
            {result.version && (
              <Badge variant="secondary" className="h-5">
                v{result.version}
              </Badge>
            )}
            {result.author && (
              <Badge variant="outline" className="h-5 max-w-[12rem]">
                <span className="truncate">{result.author}</span>
              </Badge>
            )}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  aria-label={`Details of the ${noun} ${result.name}`}
                  // Frozen while the dialog it opened is up, so the triggering
                  // control cannot stack a second one (§6 "Pending").
                  disabled={detailOpen}
                  onClick={(e) => {
                    // The row around this is the selection control; a click
                    // that reaches it picks the row, and Enter/Space reach
                    // its key handler the same way.
                    e.stopPropagation()
                    setDetailOpen(true)
                  }}
                  onKeyDown={(e) => e.stopPropagation()}
                >
                  <BookOpen className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Details
              </TooltipContent>
            </Tooltip>
          </>
        }
        flags={
          !result.supported && result.unsupportedReason ? (
            <RowFlag
              icon={AlertTriangle}
              tone="warning"
              label={result.unsupportedReason}
            />
          ) : undefined
        }
      />

      {detailOpen && (
        <AddAddonResultDetailDialog
          result={result}
          open
          onOpenChange={setDetailOpen}
        />
      )}
    </div>
  )
}
