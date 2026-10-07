import { Fragment, type ReactNode } from "react"

import type { AgentApiAccessEntry } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { pluralize } from "./apiAccess"

interface AllApiAccessSheetProps {
  /** Every person, already sorted the way the card sorts them. */
  entries: AgentApiAccessEntry[]
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The card's own row, so card and Sheet cannot drift. */
  renderRow: (entry: AgentApiAccessEntry) => ReactNode
}

/**
 * The "Show all (N)" destination for the API access card. A Sheet rather than
 * a route, by P5's test: no search, sort or pagination (the endpoint returns
 * everyone, already ordered by who is most likely wanted), and a person's
 * access has no lifecycle of its own beyond the verbs already on the row.
 */
export function AllApiAccessSheet({
  entries,
  open,
  onOpenChange,
  renderRow,
}: AllApiAccessSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>API access</SheetTitle>
          <SheetDescription>
            {pluralize(entries.length, "person", "people")} with access
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nobody has API access yet.
            </p>
          ) : (
            <ListRowGroup>
              {entries.map((entry) => (
                <Fragment key={entry.user.id}>{renderRow(entry)}</Fragment>
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
