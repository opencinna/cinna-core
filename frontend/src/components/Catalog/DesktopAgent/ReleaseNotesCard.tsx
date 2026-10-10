import { FileText } from "lucide-react"

import type { CatalogRevision } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { catalogVersionLabel } from "@/utils/catalogItems"

/**
 * The right column of the desktop agent's detail route: what changed in the
 * shown version. Release notes are commit-message prose, not markdown, so they
 * are kept as written (`whitespace-pre-wrap`) rather than rendered. The only
 * card in its column (§2 A card under a document).
 */
export function ReleaseNotesCard({
  revision,
}: {
  revision: CatalogRevision | null
}) {
  const by = revision?.published_by?.display_name

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex min-w-0 items-center gap-2">
          <FileText className="h-5 w-5 shrink-0" />
          Release notes
        </CardTitle>
        <CardDescription className="break-words">
          {revision ? (
            <>
              {catalogVersionLabel(revision)}
              {by && ` · shared by ${by}`} ·{" "}
              <RelativeTime timestamp={revision.published_at} showTooltip />
            </>
          ) : (
            "What changed in each version."
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!revision ? (
          <p className="text-sm text-muted-foreground">
            Nothing shared yet. Versions appear here after the first Share
            changes from Cinna Desktop.
          </p>
        ) : revision.release_notes ? (
          <p className="text-sm whitespace-pre-wrap break-words">
            {revision.release_notes}
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">
            No notes were written for this version.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
