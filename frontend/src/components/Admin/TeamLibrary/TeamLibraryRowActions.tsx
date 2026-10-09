import { KeyRound, Loader2, Trash2 } from "lucide-react"

import type { LibraryAgentAdmin } from "@/client"
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

interface TeamLibraryRowActionsProps {
  agent: LibraryAgentAdmin
  removing: boolean
  onEditAccess: (agent: LibraryAgentAdmin) => void
  onRemove: (agent: LibraryAgentAdmin) => void
}

/**
 * The `⋯` of one team-library row, or — while its removal is in flight — a
 * named spinner in the same slot (§2 Pending on rows). Both items only set
 * state the page owns: a `DropdownMenuItem` unmounts on select.
 */
export function TeamLibraryRowActions({
  agent,
  removing,
  onEditAccess,
  onRemove,
}: TeamLibraryRowActionsProps) {
  if (removing) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="flex h-7 w-7 items-center justify-center">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="sr-only">Removing…</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Removing…
        </TooltipContent>
      </Tooltip>
    )
  }

  return (
    <RowActionsMenu label={`the agent ${agent.name || agent.slug}`}>
      <DropdownMenuItem onSelect={() => onEditAccess(agent)}>
        <KeyRound className="mr-2 h-4 w-4" />
        Edit access
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem variant="destructive" onSelect={() => onRemove(agent)}>
        <Trash2 className="mr-2 h-4 w-4" />
        Remove from team library
      </DropdownMenuItem>
    </RowActionsMenu>
  )
}
