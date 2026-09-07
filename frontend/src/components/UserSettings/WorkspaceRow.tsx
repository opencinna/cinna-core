import { Pencil, Trash2 } from "lucide-react"

import type { UserWorkspacePublic } from "@/client"
import { ListRow } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { getWorkspaceIcon } from "@/config/workspaceIcons"

interface WorkspaceRowProps {
  workspace: UserWorkspacePublic
  onEdit: () => void
  onDelete: () => void
}

/**
 * One workspace, as a house list row.
 *
 * Rendered by both the settings card and its "Show all" Sheet, so the two
 * cannot drift. A workspace is a name and an icon and nothing else, so the row
 * is deliberately one line: no metadata, no flags, no state dot — a workspace
 * has no on/off state, and a dot that is green on every row of every list is
 * decoration.
 *
 * The mutations stay with the card rather than the row: both of them open the
 * card's own dialogs (one shared form for create and edit), so moving them here
 * would split one form across two owners.
 */
export function WorkspaceRow({
  workspace,
  onEdit,
  onDelete,
}: WorkspaceRowProps) {
  const Icon = getWorkspaceIcon(workspace.icon)

  return (
    <ListRow
      icon={
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        </span>
      }
      title={workspace.name}
    >
      <RowActionsMenu label={`the workspace ${workspace.name}`}>
        <DropdownMenuItem onSelect={onEdit}>
          <Pencil />
          Edit workspace
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => {
            e.preventDefault()
            onDelete()
          }}
        >
          <Trash2 />
          Delete workspace
        </DropdownMenuItem>
      </RowActionsMenu>
    </ListRow>
  )
}
