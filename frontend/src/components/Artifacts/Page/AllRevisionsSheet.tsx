import type { ArtifactRevisionPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { plural } from "../artifactFormat"
import { RevisionRow } from "./RevisionRow"

interface AllRevisionsSheetProps {
  artifactId: string
  artifactTitle: string
  revisions: ArtifactRevisionPublic[]
  viewedNumber: number | null
  onView: (number: number) => void
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Show all (N)" for the details panel's Revisions (the `AllHandoversSheet`
 * shape): the same row component, so the two hosts cannot drift.
 */
export function AllRevisionsSheet({
  artifactId,
  artifactTitle,
  revisions,
  viewedNumber,
  onView,
  open,
  onOpenChange,
}: AllRevisionsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Revisions</SheetTitle>
          <SheetDescription>
            {plural(revisions.length, "revision")}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {revisions.map((revision) => (
              <RevisionRow
                key={revision.id}
                artifactId={artifactId}
                artifactTitle={artifactTitle}
                revision={revision}
                isViewed={revision.number === viewedNumber}
                onView={(n) => {
                  onView(n)
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
