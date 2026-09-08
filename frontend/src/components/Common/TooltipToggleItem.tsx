import type { ReactNode } from "react"

import { ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

interface TooltipToggleItemProps {
  value: string
  /**
   * What the segment means, as a sentence. Used as the `aria-label` and —
   * unless `tooltip` overrides it — as the tooltip text, so a 3–5 character
   * segment label is still readable and still announced.
   */
  label: string
  /** Tooltip text, when it must differ from the `aria-label` (a warning). */
  tooltip?: ReactNode
  /** The short visible label. Keep it to 3–5 characters (§2 "Toggles on rows"). */
  children: ReactNode
  className?: string
}

/**
 * One segment of a segmented toggle, with its required tooltip and the
 * selected-state rule that has to hang off `aria-pressed`.
 *
 * **Why this exists at all.** A `TooltipTrigger asChild` wins the `Slot` merge:
 * Radix `Toggle` writes `data-state` *before* spreading the props it was
 * handed, and the tooltip hands its own `data-state` (open/closed) down, so the
 * tooltip's value lands last on the button. Every `data-[state=on]:*` rule on a
 * tooltipped segment is therefore dead — `toggleVariants`' own
 * `data-[state=on]:bg-accent` included — and a selected segment would look
 * exactly like an unselected one. `aria-pressed` is untouched by the tooltip.
 *
 * Two consequences worth keeping written down:
 *
 * 1. It cuts both ways. Drop the `TooltipTrigger` and `data-[state=on]:bg-accent`
 *    revives at the same specificity as the `aria-pressed` rule below, with
 *    emitted source order deciding the colour — so a caller that wants an
 *    untooltipped segment must also remove the accent rule from `ui/toggle.tsx`.
 * 2. `bg-accent` could not carry the selection anyway: it is the outline
 *    variant's *hover* colour, so on a control whose only job is to say which
 *    segments are on, hovering an off one would look like the answer.
 *    `bg-primary` is the token `Checkbox` and `Switch` already use for checked.
 *
 * Works for both group types: see the comment on the class list — the
 * attribute Radix exposes differs between `type="single"` and
 * `type="multiple"`, so the rule has to be written twice.
 *
 * Extracted at the third copy of that class block (the publish dialog, the edit
 * dialog and the installed-plugin row), which is one past where §5 asks for it.
 * `Admin/AccessPolicy/CompanyAiCredentialRow.tsx` still carries the fourth and
 * should adopt this the next time that file is touched.
 */
export function TooltipToggleItem({
  value,
  label,
  tooltip,
  children,
  className,
}: TooltipToggleItemProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <ToggleGroupItem
          value={value}
          aria-label={label}
          className={cn(
            "h-7 px-3 text-xs font-medium",
            // Both attributes, because Radix uses a different one per group
            // type: `type="multiple"` items are `aria-pressed` buttons,
            // `type="single"` items are `role="radio"` with `aria-checked` and
            // `aria-pressed` explicitly deleted. Writing only the `pressed`
            // rule leaves a single-select group with **no** selected state at
            // all — `data-[state=on]` clobbered by the tooltip, `aria-pressed`
            // never matching — which is exactly the bug this component exists
            // to stop, and which a first extraction taken from a
            // `type="multiple"` reference will not catch.
            "aria-pressed:bg-primary aria-pressed:text-primary-foreground",
            "aria-checked:bg-primary aria-checked:text-primary-foreground",
            "aria-pressed:hover:bg-primary aria-pressed:hover:text-primary-foreground",
            "aria-checked:hover:bg-primary aria-checked:hover:text-primary-foreground",
            className,
          )}
        >
          {children}
        </ToggleGroupItem>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        {tooltip ?? label}
      </TooltipContent>
    </Tooltip>
  )
}
