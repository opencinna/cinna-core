import type { SkillPackageRevisionPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { SkillRevisionRow } from "./SkillRevisionRow"

interface AllSkillRevisionsSheetProps {
  /** Every revision, newest first — the order the card previews. */
  revisions: SkillPackageRevisionPublic[]
  latestRevisionId?: string | null
  selectedRevisionNumber: number | null
  onView: (revisionNumber: number) => void
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Revisions card.
 *
 * A Sheet rather than a route (P5's route-vs-Sheet test): the full history
 * needs no search, sort or pagination, and a revision has no lifecycle of its
 * own — its one action is to swap the panel behind the Sheet, which is why
 * selecting one closes it.
 */
export function AllSkillRevisionsSheet({
  revisions,
  latestRevisionId,
  selectedRevisionNumber,
  onView,
  open,
  onOpenChange,
}: AllSkillRevisionsSheetProps) {
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
              <SkillRevisionRow
                key={rev.id}
                rev={rev}
                isLatest={latestRevisionId === rev.id}
                isSelected={selectedRevisionNumber === rev.revision_number}
                onView={(revisionNumber) => {
                  onView(revisionNumber)
                  // The panel it swaps is behind this Sheet, so leaving the
                  // Sheet open would hide the thing the click just changed.
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
