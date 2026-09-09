import { AlertTriangle, Check, GraduationCap, Store } from "lucide-react"

import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { AddAddonResult } from "@/utils/addons"

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
 * **Zero inline actions.** The whole row is the selection control, which is
 * what makes step 2 possible: with a per-row Install button there would be
 * nowhere to choose the modes without a third disclosure level. The row's only
 * other interactive parts are the flags' tooltips, which are `span`s, so
 * nothing focusable is nested inside this button.
 *
 * The row says plugin or skill, never "addon" (plan §10), and it says whichever
 * word the same entry will use once it is installed: a `skills`-format
 * marketplace entry is a skill here, in its glyph, its name and its format
 * fact, even though the install goes through the plugin endpoint.
 *
 * An already-installed result stays in the list, muted and unselectable.
 * Hiding it is what makes a user search twice for something they already have.
 *
 * So does an entry this platform cannot install: the install endpoint answers
 * one with `409 plugin_unsupported`, so it is shown with its reason and no way
 * to pick it. Hiding those instead would make an admin's half-broken
 * marketplace look empty (plan §10) — the user would search, find nothing, and
 * have nothing to take to the admin.
 */
export function AddAddonResultRow({
  result,
  selected,
  onSelect,
}: AddAddonResultRowProps) {
  // The glyph follows the *kind*, which the fold derived from the marketplace
  // format — so every row under the Skills filter carries the skill glyph, a
  // skills-format marketplace entry included. Which marketplace or publisher it
  // came from is the `origin` fact below; the tile answers "what is this".
  const Icon = result.kind === "skill" ? GraduationCap : Store
  // Two different reasons, one behaviour: the row is visible, muted and not a
  // selection control.
  const disabled = result.installed || !result.supported
  // Why it cannot be picked, in the row's *name*. An explicit `aria-label` on
  // a `role="option"` overrides the computed name, so a reason that lived only
  // in a flag would be unreachable for a reader who lands on a disabled row:
  // they would hear "Plugin Foo, dimmed" and no cause at all. Installed says
  // so too — it was the half that had no reason before.
  const blockedReason = result.installed
    ? "already installed in this agent"
    : result.unsupportedReason

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
        "rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring",
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
          result.version ? (
            <Badge variant="secondary" className="h-5">
              v{result.version}
            </Badge>
          ) : undefined
        }
        flags={
          <>
            {result.installed && (
              <RowFlag icon={Check} label="Already installed in this agent" />
            )}
            {!result.supported && result.unsupportedReason && (
              <RowFlag
                icon={AlertTriangle}
                tone="warning"
                label={result.unsupportedReason}
              />
            )}
            {/* The reason is NOT repeated here. `RowFlag` already announces
                it three ways — glyph, tooltip and its own `sr-only`
                (`ListRow.tsx`) — and the row's `aria-label` carries it too;
                a fourth copy is a second tooltip in the same cluster saying
                the identical sentence. `RowInfo` is for facts that are true
                but not scanned for, and the reason a row refuses to be picked
                is scanned for the instant the user clicks it. */}
            <RowInfo
              facts={[result.description, result.origin, ...result.facts]}
            />
          </>
        }
      />
    </div>
  )
}
