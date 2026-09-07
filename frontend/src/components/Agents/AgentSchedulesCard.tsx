import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { CalendarClock, Plus } from "lucide-react"

import type { AgentSchedulePublic } from "@/client"
import { AgentsService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AllSchedulesSheet } from "./AllSchedulesSheet"
import { CreateScheduleDialog } from "./CreateScheduleDialog"
import { ScheduleRow } from "./ScheduleRow"
import { nextExecutionSortKey } from "./scheduleFormatting"

/** Rows shown in the card; the rest live behind "Show all (N)". */
const PREVIEW_COUNT = 5

/** Stable identity so the sort memoises while the query loads. */
const NO_SCHEDULES: AgentSchedulePublic[] = []

interface AgentSchedulesCardProps {
  agentId: string
  /**
   * When true, the schedule definitions are bundle-authored and the consumer
   * may not create, edit or delete them. Enable/disable, Run now and the
   * execution logs stay available (the backend allows a PUT carrying only
   * `{enabled}` on foreign installs).
   */
  readOnly?: boolean
}

/**
 * "Schedules" — what runs on a timer for this agent.
 *
 * A View surface (guidelines §1): the card answers "what is scheduled, is it
 * live, when does the next one fire" at a glance, and carries the two things
 * a user actually opens it for — kick a run off, check what the last one did.
 * Everything that changes a schedule's definition is one level down, in the
 * row's `⋯` menu, so nothing on the card body changes its height and the card
 * holds one interaction model (auto-save) while the forms live in dialogs.
 *
 * Blocks: header (1) + the row list (1, capped at 5 with "Show all") — well
 * inside §2's seven. The `readOnly` notice is not a block of its own: it
 * *replaces* the card description rather than adding a sentence under it.
 */
export function AgentSchedulesCard({
  agentId,
  readOnly = false,
}: AgentSchedulesCardProps) {
  const [isCreateOpen, setIsCreateOpen] = useState(false)
  const [isSheetOpen, setIsSheetOpen] = useState(false)

  const {
    data: schedulesData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["agent-schedules", agentId],
    queryFn: () => AgentsService.listSchedules({ id: agentId }),
    enabled: !!agentId,
  })

  const schedules = schedulesData?.data ?? NO_SCHEDULES
  const totalCount = schedulesData?.count ?? schedules.length

  // Most relevant first: live schedules, then soonest to fire within each
  // group. A disabled schedule's next run is not shown on the row, but it
  // still orders the disabled block sensibly.
  const sortedSchedules = useMemo(
    () =>
      [...schedules].sort((a, b) => {
        if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
        return (
          nextExecutionSortKey(a.next_execution) -
          nextExecutionSortKey(b.next_execution)
        )
      }),
    [schedules],
  )

  const emptyState = (
    <p className="text-sm text-muted-foreground">
      {readOnly
        ? "The bundle publisher hasn't shipped any schedules with this agent."
        : "No schedules yet. Add one to run this agent on a timer."}
    </p>
  )

  return (
    <Card>
      <CardHeader>
        {/* Title and action share one row; the description spans the full
            header width below them, so it stays one or two lines at 1024
            instead of being squeezed into a column beside the button. */}
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <CalendarClock className="h-5 w-5 shrink-0" />
            Schedules
          </CardTitle>
          {!readOnly && (
            <div className="shrink-0">
              <Button size="sm" onClick={() => setIsCreateOpen(true)}>
                <Plus className="mr-2 h-4 w-4" />
                New schedule
              </Button>
            </div>
          )}
        </div>
        <CardDescription>
          {/* In read-only mode the description *is* the mode notice: a second
              sentence under the header would spend a block on chrome. */}
          {readOnly
            ? "Managed by the bundle publisher — you can enable, run and view logs."
            : "Timed runs for this agent — a prompt or a script, on a cadence."}
        </CardDescription>
      </CardHeader>

      <CardContent>
        {/* The cap, the "Show all" link and the four states are the shared P5
            primitive's job; the row and the copy stay here. `isError` is
            gated on there being no data to show, so a failed *background*
            refetch keeps the rows on screen instead of blanking the card —
            a first failed load still renders as a failure, never as "no
            schedules yet". */}
        <PreviewList
          items={sortedSchedules}
          total={totalCount}
          previewCount={PREVIEW_COUNT}
          getKey={(schedule) => schedule.id}
          renderItem={(schedule) => (
            <ScheduleRow
              agentId={agentId}
              schedule={schedule}
              readOnly={readOnly}
            />
          )}
          isLoading={isLoading}
          isError={isError && !schedulesData}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load schedules"
          empty={emptyState}
          onShowAll={() => setIsSheetOpen(true)}
        />
      </CardContent>

      {/* Owner-only: a consumer of a bundle-authored agent cannot create
          schedules, so neither the trigger nor the dialog is mounted. */}
      {!readOnly && (
        <CreateScheduleDialog
          agentId={agentId}
          open={isCreateOpen}
          onOpenChange={setIsCreateOpen}
        />
      )}

      <AllSchedulesSheet
        agentId={agentId}
        schedules={sortedSchedules}
        totalCount={totalCount}
        readOnly={readOnly}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />
    </Card>
  )
}
