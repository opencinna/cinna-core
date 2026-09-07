import { Power, PowerOff } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

interface OnOffToggleProps {
  checked: boolean
  onChange: (next: boolean) => void
  /** Names the entity being switched: "Google Chat", "Nightly sync". */
  label: string
  disabled?: boolean
  className?: string
}

/**
 * The inline enable/disable control for a list row whose *purpose* is being on
 * or off (guidelines §2 "Toggles on rows").
 *
 * A `Power` / `PowerOff` ghost icon button — the same glyph pair the `⋯` menus
 * already use for Enable / Disable, so the action reads identically whether it
 * is promoted to the row or left in the menu. Not a `Switch`: a switch is a
 * wide grey pill whose meaning is carried entirely by which end the knob sits
 * at, so a list of them is a list of identical shapes, and it duplicates (and
 * during a pending write can contradict) the state dot the row already has.
 *
 * The glyph names the **action**, not the state — `PowerOff` on a row that is
 * currently on — because the state is the dot's job and the button's job is to
 * say what clicking it does. The tooltip says it in words.
 *
 * It is one inline action for the budget, which leaves room for the `⋯` menu
 * beside it and nothing else. Where toggling is *not* the row's purpose (most
 * lists), there is no control here: Enable / Disable stays a menu item.
 */
export function OnOffToggle({
  checked,
  onChange,
  label,
  disabled,
  className,
}: OnOffToggleProps) {
  const Icon = checked ? PowerOff : Power
  const action = checked ? "Disable" : "Enable"

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn("h-7 w-7", className)}
          disabled={disabled}
          aria-label={`${action} ${label}`}
          onClick={() => onChange(!checked)}
        >
          <Icon className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {action}
      </TooltipContent>
    </Tooltip>
  )
}
