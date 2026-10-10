import { FileText } from "lucide-react"
import type { MouseEvent } from "react"

import type { CatalogRevision } from "@/client"
import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { catalogVersionLabel } from "@/utils/catalogItems"

interface CatalogRevisionRowProps {
  revision: CatalogRevision
  isLatest: boolean
  /** Pin this version into the release-notes card behind the Sheet. */
  onView: (revisionNumber: number) => void
}

/**
 * One shared version of a catalog item, as a house list row whose **whole
 * row** is the control (§2 Row as the control): there is nothing else to do
 * with a version than to read it. No `rounded-*` on the wrapper — the group's
 * hairline is a border on it — and the click is guarded against portaled
 * content bubbling up the React tree.
 */
export function CatalogRevisionRow({
  revision,
  isLatest,
  onView,
}: CatalogRevisionRowProps) {
  const label = catalogVersionLabel(revision)
  const when = formatRelativeTimestamp(revision.published_at)
  const by = revision.published_by?.display_name
  const meta = [by && `shared by ${by}`, when].filter(Boolean).join(" · ")

  const open = (e: MouseEvent<HTMLDivElement>) => {
    if (!e.currentTarget.contains(e.target as Node)) return
    if ((e.target as HTMLElement).closest("button")) return
    onView(revision.revision_number)
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the catalog cards make.
    <div
      role="button"
      tabIndex={0}
      aria-label={`Read the notes of ${label}`}
      className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      onClick={open}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        onView(revision.revision_number)
      }}
    >
      <ListRow
        // Append-only history: "latest" is the one state a version has. Every
        // row carries the dot (muted on earlier versions, as `SkillRevisionRow`
        // does) so the titles stay on one vertical line.
        status={
          isLatest
            ? { tone: "on", label: "Latest version" }
            : { tone: "off", label: "Earlier version" }
        }
        title={label}
        meta={meta || undefined}
        flags={
          revision.release_notes ? (
            <RowFlag icon={FileText} label={revision.release_notes} />
          ) : undefined
        }
      />
    </div>
  )
}
