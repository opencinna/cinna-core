import { formatDistanceToNow } from "date-fns"
import { Eye, FileText } from "lucide-react"

import type { SkillPackageRevisionPublic } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatSkillBytes, skillRevisionLabel } from "@/utils/skillCatalog"

interface SkillRevisionRowProps {
  rev: SkillPackageRevisionPublic
  /** This revision is the package's `latest_revision_id`. */
  isLatest: boolean
  /** Its source is the one the "SKILL.md" card is currently showing. */
  isSelected: boolean
  onView: (revisionNumber: number) => void
}

/**
 * One published revision, as a house list row.
 *
 * Rendered by both the Revisions card and its "Show all" Sheet, so the two
 * cannot drift — the shape `BundleRevisionRow` established for exactly this
 * entity. Revisions are immutable, so there is no `⋯`: the row's one action is
 * its purpose, and it is a read-only expand of the panel beside it rather than
 * a mutation.
 */
export function SkillRevisionRow({
  rev,
  isLatest,
  isSelected,
  onView,
}: SkillRevisionRowProps) {
  const label = skillRevisionLabel(rev)

  let publishedLabel = "published at an unknown time"
  try {
    publishedLabel = `published ${formatDistanceToNow(
      new Date(rev.published_at),
      { addSuffix: true },
    )}`
  } catch {
    // The row still has to render; a bad timestamp is not a reason to lose it.
  }

  return (
    <ListRow
      // Append-only history, so "latest" is the one state a revision has — the
      // leading dot rather than a badge the eye has to read on every row to
      // find the one row it is not on.
      status={{
        tone: isLatest ? "on" : "off",
        label: isLatest ? "Latest revision" : "Superseded revision",
      }}
      title={
        <>
          {label}
          {rev.version && (
            <span className="ml-1.5 text-xs font-normal text-muted-foreground">
              rev {rev.revision_number}
            </span>
          )}
        </>
      }
      meta={`${publishedLabel} · ${formatSkillBytes(rev.size_bytes)}`}
      flags={
        <>
          {/* Release notes are prose of unknown length: as a third line they
              would set the height of every row off the longest one. */}
          {rev.release_notes && (
            <RowFlag icon={FileText} label={rev.release_notes} />
          )}
          <RowInfo
            facts={[
              rev.content_hash && `Content hash ${rev.content_hash}`,
              `Revision ${rev.revision_number}`,
            ]}
          />
        </>
      }
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`Show the SKILL.md of ${label}`}
            aria-pressed={isSelected}
            disabled={isSelected}
            onClick={() => onView(rev.revision_number)}
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          {isSelected ? "Shown in the SKILL.md panel" : "View this revision"}
        </TooltipContent>
      </Tooltip>
    </ListRow>
  )
}
