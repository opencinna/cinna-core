import { History } from "lucide-react"
import { useState } from "react"

import type { SkillPackageRevisionPublic } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AllSkillRevisionsSheet } from "./AllSkillRevisionsSheet"
import { SkillRevisionRow } from "./SkillRevisionRow"

interface SkillRevisionsCardProps {
  revisions: SkillPackageRevisionPublic[]
  latestRevisionId?: string | null
  selectedRevisionNumber: number | null
  onView: (revisionNumber: number) => void
}

/**
 * "What versions of this package exist" — the third card of S7.
 *
 * A P5 preview list: five rows and a "Show all (N)" Sheet, because a package
 * that has been republished weekly for a year must not grow this card by a row
 * a week. There is no loading or error branch of its own — the route holds one
 * query for the whole package and renders its skeletons and its
 * `QueryErrorAlert` at that level, so this card only ever sees loaded data.
 */
export function SkillRevisionsCard({
  revisions,
  latestRevisionId,
  selectedRevisionNumber,
  onView,
}: SkillRevisionsCardProps) {
  const [sheetOpen, setSheetOpen] = useState(false)

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <History className="h-5 w-5 shrink-0" />
          Revisions
        </CardTitle>
        <CardDescription>
          Every publish of this skill, newest first. Revisions are immutable —
          an install pins one.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <PreviewList
          items={revisions}
          getKey={(rev) => rev.id}
          renderItem={(rev) => (
            <SkillRevisionRow
              rev={rev}
              isLatest={latestRevisionId === rev.id}
              isSelected={selectedRevisionNumber === rev.revision_number}
              onView={onView}
            />
          )}
          errorFallback="Couldn't load revisions"
          empty={
            <p className="text-sm text-muted-foreground">
              This package has no published revision yet.
            </p>
          }
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <AllSkillRevisionsSheet
        revisions={revisions}
        latestRevisionId={latestRevisionId}
        selectedRevisionNumber={selectedRevisionNumber}
        onView={onView}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
    </Card>
  )
}
