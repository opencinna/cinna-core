import type { AgentBundleRevisionPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { BundleRevisionRow } from "./BundleRevisionRow"

interface AllRevisionsSheetProps {
  agentId: string
  bundleUuid: string
  /** Every revision, newest first — the order the card previews. */
  revisions: AgentBundleRevisionPublic[]
  currentRevisionId?: string | null
  installedRevisionNumber?: number | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Revisions card.
 *
 * A Sheet rather than a route ([P5](../../../docs/development/frontend/ui_ux_guidelines.md)):
 * the full history needs no search, sort or pagination, and a revision has no
 * lifecycle of its own beyond the two actions its row already carries.
 */
export function AllRevisionsSheet({
  agentId,
  bundleUuid,
  revisions,
  currentRevisionId,
  installedRevisionNumber,
  open,
  onOpenChange,
}: AllRevisionsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Revisions</SheetTitle>
          <SheetDescription>
            {revisions.length} published revision
            {revisions.length === 1 ? "" : "s"}, newest first
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {revisions.map((rev) => (
              <BundleRevisionRow
                key={rev.id}
                agentId={agentId}
                bundleUuid={bundleUuid}
                rev={rev}
                isCurrent={currentRevisionId === rev.id}
                isInstalled={installedRevisionNumber === rev.revision_number}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
