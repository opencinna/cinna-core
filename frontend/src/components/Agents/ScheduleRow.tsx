import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  History,
  Pencil,
  Play,
  Power,
  PowerOff,
  Terminal,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import type { AgentSchedulePublic } from "@/client"
import { AgentsService } from "@/client"
import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { EditScheduleDialog } from "./EditScheduleDialog"
import { ScheduleLogsDialog } from "./ScheduleLogsDialog"
import { formatNextExecutionShort } from "./scheduleFormatting"

interface ScheduleRowProps {
  agentId: string
  schedule: AgentSchedulePublic
  /**
   * Bundle-authored schedule on a foreign install: the consumer may enable,
   * disable, run and read logs, but not create, edit or delete.
   */
  readOnly?: boolean
}

/**
 * One schedule, as a compact P3 row.
 *
 * Rendered by both the card and the "Show all" Sheet so the two hosts cannot
 * drift. The two inline actions are the reason the user opens this card
 * mid-day — kick a run off, check what the last one did — and everything that
 * changes the schedule's definition is in the `⋯` menu behind them.
 *
 * Run, toggle and delete are owned by the row rather than the card. That is
 * the point of the split, not a side effect: pending state is then per row
 * (one Run no longer disables every row's Run button), and the Sheet's rows
 * behave identically to the card's with no second wiring.
 *
 * The row also owns its three overlays. A `DropdownMenuItem` unmounts on
 * select, so a dialog trigger nested in the menu would take the pending state
 * with it; the menu items set state instead, and the dialogs open over the
 * Sheet without closing it.
 *
 * One deliberate divergence from `HandoverRow`, which this file otherwise
 * mirrors: errors go through `getErrorMessage(err, <fallback>)` rather than
 * `handleError.bind(showErrorToast)`. Four distinct verbs act on one row here,
 * and `extractErrorMessage` bottoms out at a generic "Something went wrong."
 * where a per-call fallback can say which one failed.
 */
export function ScheduleRow({
  agentId,
  schedule,
  readOnly = false,
}: ScheduleRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [editOpen, setEditOpen] = useState(false)
  const [logsOpen, setLogsOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["agent-schedules", agentId] })

  const runMutation = useMutation({
    mutationFn: () =>
      AgentsService.runScheduleNow({ id: agentId, scheduleId: schedule.id }),
    onSuccess: (response) => {
      // The backend's message is context-aware: a synchronous run confirms
      // success, while a suspended environment answers "starting…" so the
      // user knows the run was deferred rather than lost.
      showSuccessToast(response.message || "Schedule triggered")
      invalidate()
      // A deferred run flips the environment to `activating`; without this the
      // agent page's status indicator contradicts the toast.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      // The run this just started belongs in the logs; without it the dialog
      // shows the pre-run cache for a frame when it is next opened.
      queryClient.invalidateQueries({
        queryKey: ["schedule-logs", schedule.id],
      })
    },
    onError: (err: unknown) =>
      showErrorToast(getErrorMessage(err, "Failed to trigger schedule")),
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateSchedule({
        id: agentId,
        scheduleId: schedule.id,
        requestBody: { enabled },
      }),
    onSuccess: (_data, enabled) => {
      showSuccessToast(enabled ? "Schedule enabled" : "Schedule disabled")
      invalidate()
    },
    onError: (err: unknown) =>
      showErrorToast(getErrorMessage(err, "Failed to toggle schedule")),
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      AgentsService.deleteSchedule({ id: agentId, scheduleId: schedule.id }),
    onSuccess: () => {
      showSuccessToast(`Schedule "${schedule.name}" deleted`)
      setConfirmOpen(false)
      invalidate()
    },
    onError: (err: unknown) =>
      showErrorToast(getErrorMessage(err, "Failed to delete schedule")),
  })

  // Pending is scoped to this row: only the row being mutated loses its
  // controls.
  const isPending =
    runMutation.isPending ||
    toggleMutation.isPending ||
    deleteMutation.isPending

  const isScript = schedule.schedule_type === "script_trigger"

  // ≤ 2 facts, joined by " · ". `description` is the AI-authored cadence
  // sentence — it *is* the cadence fact, so the cron string never appears on
  // a row. A disabled schedule's computed next run is misinformation, not
  // data, so it is left out.
  const metadata =
    schedule.enabled && schedule.next_execution
      ? `${schedule.description} · Next ${formatNextExecutionShort(schedule.next_execution)}`
      : schedule.description

  return (
    <ListRow
      muted={!schedule.enabled}
      // State was an "Off" badge on the minority of rows; it is now the dot on
      // every row, which costs the name nothing and reads the same way in
      // every list in the product.
      status={{
        tone: schedule.enabled ? "on" : "off",
        label: schedule.enabled ? "Enabled" : "Disabled",
      }}
      title={schedule.name}
      meta={metadata}
      flags={
        // The trigger kind, only when it is not the default static prompt: a
        // glyph on the majority of rows is decoration. The command it runs is
        // in the tooltip, which is the only place it fits.
        isScript ? (
          <RowFlag
            icon={Terminal}
            label={
              schedule.command
                ? `Script trigger — ${schedule.command}`
                : "Script trigger"
            }
          />
        ) : undefined
      }
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={isPending}
            onClick={() => runMutation.mutate()}
            aria-label={`Run ${schedule.name} now`}
          >
            <Play className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Run now
        </TooltipContent>
      </Tooltip>

      <RowActionsMenu
        label={`the schedule ${schedule.name}`}
        disabled={isPending}
      >
        {!readOnly && (
          <DropdownMenuItem
            onSelect={(e) => {
              e.preventDefault()
              setEditOpen(true)
            }}
          >
            <Pencil />
            Edit schedule
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault()
            setLogsOpen(true)
          }}
        >
          <History />
          Execution logs
        </DropdownMenuItem>
        {/* Reversible, so it toasts its result instead of confirming. */}
        <DropdownMenuItem
          onSelect={() => toggleMutation.mutate(!schedule.enabled)}
        >
          {schedule.enabled ? <PowerOff /> : <Power />}
          {schedule.enabled ? "Disable" : "Enable"}
        </DropdownMenuItem>
        {!readOnly && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={(e) => {
                e.preventDefault()
                setConfirmOpen(true)
              }}
            >
              <Trash2 />
              Delete schedule
            </DropdownMenuItem>
          </>
        )}
      </RowActionsMenu>

      {/* Mounted only while open: a resident instance would re-seed its form
          from a background refetch and lose the user's in-progress edit. */}
      {editOpen && (
        <EditScheduleDialog
          agentId={agentId}
          schedule={schedule}
          open
          onClose={() => setEditOpen(false)}
        />
      )}

      {logsOpen && (
        <ScheduleLogsDialog
          agentId={agentId}
          schedule={schedule}
          open
          onClose={() => setLogsOpen(false)}
        />
      )}

      {/* Symmetry with the menu item that opens it: a consumer of a
          bundle-authored schedule has no delete path, so it mounts nothing. */}
      {!readOnly && (
        <AlertDialog
          open={confirmOpen}
          // Escape would otherwise close the confirm mid-request, leaving the
          // pending state with nowhere to live.
          onOpenChange={(next) => {
            if (!deleteMutation.isPending) setConfirmOpen(next)
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete schedule</AlertDialogTitle>
              <AlertDialogDescription>
                Delete <strong>{schedule.name}</strong>? It will stop running
                and this action cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={deleteMutation.isPending}>
                Cancel
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={(e) => {
                  // Keep the confirm open while the request is in flight so
                  // the pending state has somewhere to live.
                  e.preventDefault()
                  deleteMutation.mutate()
                }}
                disabled={deleteMutation.isPending}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {deleteMutation.isPending ? "Deleting…" : "Delete"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </ListRow>
  )
}
