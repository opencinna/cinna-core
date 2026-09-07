import type { AgentSchedulePublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { ScheduleRow } from "./ScheduleRow"

interface AllSchedulesSheetProps {
  agentId: string
  /** Every schedule, already sorted the way the card sorts them. */
  schedules: AgentSchedulePublic[]
  /**
   * The list's N, read from the same `count` the card's "Show all (N)" uses,
   * so the trigger and the header it opens always print the same number.
   */
  totalCount: number
  readOnly?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Schedules card.
 *
 * A Sheet rather than a route: schedules have no route of their own, the full
 * list needs no search, sort or pagination (an agent has a handful), and the
 * per-row execution history is reachable from the row itself. The rows are
 * the card's rows — same component, same actions — so the two hosts cannot
 * drift, and the row's own dialogs open over the Sheet rather than closing it.
 */
export function AllSchedulesSheet({
  agentId,
  schedules,
  totalCount,
  readOnly = false,
  open,
  onOpenChange,
}: AllSchedulesSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Schedules</SheetTitle>
          <SheetDescription>{totalCount} schedules</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {schedules.length === 0 ? (
            // Reachable without closing the Sheet: the rows carry Delete.
            <p className="text-sm text-muted-foreground">
              {readOnly
                ? "The bundle publisher hasn't shipped any schedules with this agent."
                : "No schedules yet. Add one to run this agent on a timer."}
            </p>
          ) : (
            <ListRowGroup>
              {schedules.map((schedule) => (
                <ScheduleRow
                  key={schedule.id}
                  agentId={agentId}
                  schedule={schedule}
                  readOnly={readOnly}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
