import { MessageSquare } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { DASHBOARD_CHAT_TOGGLE_ID } from "./DashboardChatPanel"

/**
 * The page-header toggle for the dashboard chat (UI spec S5). Its label is the
 * same whatever its state (§6: a label is not a state readout); the state is
 * `aria-pressed`. In edit mode there is no chat, and the tooltip says why.
 */
export function DashboardChatToggle({
  open,
  onToggle,
  isEditMode,
}: {
  open: boolean
  onToggle: () => void
  isEditMode: boolean
}) {
  const pressed = open && !isEditMode
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A disabled button gets no pointer events; the span keeps the tooltip. */}
        <span className="inline-flex">
          <Button
            id={DASHBOARD_CHAT_TOGGLE_ID}
            size="icon"
            variant={pressed ? "secondary" : "outline"}
            className="h-8 w-8"
            aria-pressed={pressed}
            aria-label="Dashboard chat"
            disabled={isEditMode}
            onClick={onToggle}
          >
            <MessageSquare className="h-4 w-4" />
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="text-xs">
        {isEditMode ? "Lock the layout to chat" : "Dashboard chat"}
      </TooltipContent>
    </Tooltip>
  )
}
