import { Fragment, type ReactNode } from "react"

import type { AgentApiAccessEntry, AgentApiAccessGroupEntry } from "@/client"
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
  /** Every group holding scopes, sorted by name (the server's order). */
  groups: AgentApiAccessGroupEntry[]
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The card's own rows, so card and Sheet cannot drift. */
  renderRow: (entry: AgentApiAccessEntry) => ReactNode
  renderGroupRow: (group: AgentApiAccessGroupEntry) => ReactNode
}

/**
 * The "Show all (N)" destination for the API access card. A Sheet rather than
 * a route, by P5's test: no search, sort or pagination (the endpoint returns
 * everyone, already ordered by who is most likely wanted), and a person's
 * access has no lifecycle of its own beyond the verbs already on the row.
 * People first, then groups, under the same two headings as the card.
 */
export function AllApiAccessSheet({
  entries,
  groups,
  open,
  onOpenChange,
  renderRow,
  renderGroupRow,
}: AllApiAccessSheetProps) {
  const counts = [
    entries.length > 0 || groups.length === 0
      ? pluralize(entries.length, "person", "people")
      : null,
    groups.length > 0 ? pluralize(groups.length, "group", "groups") : null,
  ]
    .filter(Boolean)
    .join(" · ")

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>API access</SheetTitle>
          <SheetDescription>{counts} with access</SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-y-auto px-4 pb-4">
          {entries.length === 0 && groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nobody has API access yet.
            </p>
          ) : (
            <>
              {entries.length > 0 && (
                <section className="space-y-2">
                  {groups.length > 0 && (
                    <h3 className="text-sm font-medium">People</h3>
                  )}
                  <ListRowGroup>
                    {entries.map((entry) => (
                      <Fragment key={entry.user.id}>
                        {renderRow(entry)}
                      </Fragment>
                    ))}
                  </ListRowGroup>
                </section>
              )}
              {groups.length > 0 && (
                <section className="space-y-2">
                  <h3 className="text-sm font-medium">Groups</h3>
                  <ListRowGroup>
                    {groups.map((group) => (
                      <Fragment key={group.grant_id}>
                        {renderGroupRow(group)}
                      </Fragment>
                    ))}
                  </ListRowGroup>
                </section>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
