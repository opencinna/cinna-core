import { Ban, Eye, Loader2, Pencil, Trash } from "lucide-react"

import type { McpDirectoryEntryPublic } from "@/client"
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
import { useRowPending } from "./mcpDirectoryApi"

export interface McpEntryActions {
  onEdit: (entry: McpDirectoryEntryPublic) => void
  onBan: (entry: McpDirectoryEntryPublic) => void
  onUnban: (entry: McpDirectoryEntryPublic) => void
  onDelete: (entry: McpDirectoryEntryPublic) => void
}

/**
 * The row's `⋯`: Edit · Ban… / Unban · Delete. The dialogs it opens are owned
 * by the list (a menu item unmounts on select). While a write on this row is
 * in flight the menu's slot shows a spinner naming it.
 */
export function McpEntryActionsMenu({
  entry,
  actions,
}: {
  entry: McpDirectoryEntryPublic
  actions: McpEntryActions
}) {
  const pending = useRowPending(entry)

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

  const banned = entry.access === "banned"

  return (
    <RowActionsMenu label={`the MCP server ${entry.title}`}>
      <DropdownMenuItem onSelect={() => actions.onEdit(entry)}>
        <Pencil />
        Edit
      </DropdownMenuItem>
      {banned ? (
        <DropdownMenuItem onSelect={() => actions.onUnban(entry)}>
          <Eye />
          Unban
        </DropdownMenuItem>
      ) : (
        <DropdownMenuItem onSelect={() => actions.onBan(entry)}>
          <Ban />
          Ban…
        </DropdownMenuItem>
      )}
      <DropdownMenuSeparator />
      <DropdownMenuItem
        variant="destructive"
        onSelect={() => actions.onDelete(entry)}
      >
        <Trash />
        Delete
      </DropdownMenuItem>
    </RowActionsMenu>
  )
}
