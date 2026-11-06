import type { BrandThemeDraftPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { BrandThemeDraftRow } from "./BrandThemeDraftRow"
import { draftsSummary } from "./draftsSummary"

interface AllBrandThemeDraftsSheetProps {
  /** Every stored theme, newest first, as the card holds them. */
  drafts: BrandThemeDraftPublic[]
  count: number
  maxDrafts: number | undefined
  onPreview: (draft: BrandThemeDraftPublic) => void
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The Drafts card's "Show all (N)". A Sheet rather than a route: at most 20
 * items, no search, sort or paging. The rows are the card's rows, so the two
 * hosts cannot drift.
 */
export function AllBrandThemeDraftsSheet({
  drafts,
  count,
  maxDrafts,
  onPreview,
  open,
  onOpenChange,
}: AllBrandThemeDraftsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Drafts</SheetTitle>
          <SheetDescription>{draftsSummary(count, maxDrafts)}</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto pb-4 pl-4 pr-6">
          {drafts.length === 0 ? (
            // Reachable without closing the Sheet: the rows carry Delete.
            <p className="text-sm text-muted-foreground">No drafts yet.</p>
          ) : (
            <ListRowGroup>
              {drafts.map((draft) => (
                <BrandThemeDraftRow
                  key={draft.id}
                  draft={draft}
                  onPreview={onPreview}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
