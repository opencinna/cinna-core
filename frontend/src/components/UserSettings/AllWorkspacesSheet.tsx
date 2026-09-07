import type { UserWorkspacePublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { WorkspaceRow } from "./WorkspaceRow"

interface AllWorkspacesSheetProps {
  workspaces: UserWorkspacePublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onEdit: (workspace: UserWorkspacePublic) => void
  onDelete: (workspace: UserWorkspacePublic) => void
}

/**
 * The "Show all (N)" destination for the Workspaces card.
 *
 * A Sheet, not a route (P5's test): workspaces have no page of their own, the
 * full list needs no search, sort or pagination, and a workspace has no
 * lifecycle beyond the two actions its row already carries.
 */
export function AllWorkspacesSheet({
  workspaces,
  open,
  onOpenChange,
  onEdit,
  onDelete,
}: AllWorkspacesSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Workspaces</SheetTitle>
          <SheetDescription>
            {workspaces.length} workspace{workspaces.length === 1 ? "" : "s"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {workspaces.map((workspace) => (
              <WorkspaceRow
                key={workspace.id}
                workspace={workspace}
                onEdit={() => onEdit(workspace)}
                onDelete={() => onDelete(workspace)}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
