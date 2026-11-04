import { useState } from "react"

import type { ComponentPrompt } from "@/components/Dashboard/AgentComponents/componentSpec"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"

const SHOWN = 3
const PILL =
  "inline-flex max-w-full items-center rounded-full border px-3 py-1 text-xs hover:bg-muted outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"

/**
 * The focused tile's `prompts[]` above the composer (UI spec S5): ≤ 3 pills,
 * the rest behind "More (N)". A click sends the prompt with the tile's
 * context (D13); it never only fills the composer.
 */
export function PromptStarters({
  prompts,
  onSend,
  disabled,
}: {
  prompts: ComponentPrompt[]
  onSend: (prompt: string) => void
  disabled?: boolean
}) {
  const [moreOpen, setMoreOpen] = useState(false)
  if (prompts.length === 0) return null
  const shown = prompts.slice(0, SHOWN)
  const rest = prompts.slice(SHOWN)

  return (
    <div className="flex flex-wrap gap-1.5 px-3 pb-2">
      {shown.map((p) => (
        <button
          key={p.label}
          type="button"
          className={PILL}
          disabled={disabled}
          onClick={() => onSend(p.prompt)}
        >
          <span className="truncate">{p.label}</span>
        </button>
      ))}
      {rest.length > 0 && (
        <Popover open={moreOpen} onOpenChange={setMoreOpen}>
          <PopoverTrigger asChild>
            <button type="button" className={PILL} disabled={disabled}>
              More ({rest.length})
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 p-1">
            <div className="max-h-[240px] overflow-y-auto pr-2">
              {rest.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  className="block w-full truncate rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-muted focus-visible:bg-muted"
                  onClick={() => {
                    setMoreOpen(false)
                    onSend(p.prompt)
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>
      )}
    </div>
  )
}
