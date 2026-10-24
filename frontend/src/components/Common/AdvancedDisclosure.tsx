import { ChevronDown, ChevronRight } from "lucide-react"
import { type ReactNode, useState } from "react"

import { Button } from "@/components/ui/button"

/**
 * One collapsed "Advanced" toggle over a form's optional fields (§1 Create:
 * optional with a default → Advanced). Starts closed on every mount; a
 * refused field inside passes `forceOpen` so it cannot stay hidden.
 */
export function AdvancedDisclosure({
  children,
  forceOpen = false,
}: {
  children: ReactNode
  forceOpen?: boolean
}) {
  const [toggled, setOpen] = useState(false)
  const open = toggled || forceOpen
  return (
    <div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="px-0 text-muted-foreground"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        Advanced
      </Button>
      {open && <div className="pt-2">{children}</div>}
    </div>
  )
}
