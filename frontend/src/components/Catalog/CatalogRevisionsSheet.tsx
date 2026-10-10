import type { CatalogRevision } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { CatalogRevisionRow } from "./CatalogRevisionRow"

interface CatalogRevisionsSheetProps {
  /** Every version, newest first. */
  revisions: CatalogRevision[]
  latestRevisionNumber: number | null
  onView: (revisionNumber: number) => void
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * S2a — every shared version of a catalog item, opened from its Version fact.
 * The shape of `AllSkillRevisionsSheet` over `CatalogRevision`: a Sheet, not a
 * route (no search, no lifecycle of its own), and choosing a row closes it,
 * because the card it changes is behind it.
 */
export function CatalogRevisionsSheet({
  revisions,
  latestRevisionNumber,
  onView,
  open,
  onOpenChange,
}: CatalogRevisionsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Versions</SheetTitle>
          <SheetDescription>
            {revisions.length} shared{" "}
            {revisions.length === 1 ? "version" : "versions"}, newest first
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {revisions.map((revision) => (
              <CatalogRevisionRow
                key={revision.revision_number}
                revision={revision}
                isLatest={revision.revision_number === latestRevisionNumber}
                onView={(revisionNumber) => {
                  onView(revisionNumber)
                  onOpenChange(false)
                }}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
