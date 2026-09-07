import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, Check, Clock, Sparkles } from "lucide-react"

import type { AgentSchedulePublic, UpdateScheduleRequest } from "@/client"
import { AgentsService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { formatNextExecution } from "./scheduleFormatting"

interface GeneratedTiming {
  description: string
  cron_string: string
  next_execution: string
}

interface EditScheduleDialogProps {
  agentId: string
  schedule: AgentSchedulePublic
  open: boolean
  onClose: () => void
}

/**
 * "Show me this schedule's whole configuration and let me change it."
 *
 * One section, three fields ⇒ a `Dialog` (§1 Edit). The card's own controls
 * auto-save; this form is one level down with explicit Save / Cancel and
 * dirty tracking, so the two interaction models never share a surface.
 *
 * Mounted only while open, by every caller. That is load-bearing: the form
 * seeds from `schedule`, which comes straight from the list query, so a
 * mounted-but-closed instance would re-seed from a background refetch and
 * silently discard whatever the user had typed.
 *
 * Deliberate deviation from `EditHandoverPromptModal`'s react-hook-form
 * skeleton: Timing is not a plain input but a generate-then-accept
 * interaction whose committed value is derived state, and the save already
 * diffs against the loaded schedule. RHF would add a resolver and buy nothing
 * the diff does not already give.
 */
export function EditScheduleDialog({
  agentId,
  schedule,
  open,
  onClose,
}: EditScheduleDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone
  /**
   * The row as it was when the dialog opened.
   *
   * Mounting only while open freezes the *seed* values, but the diff below
   * needs a frozen baseline too: `schedule` is the live list-query row, so a
   * rename landing from another tab, the CLI or a bundle update would move
   * the goalposts under a form the user is mid-way through — marking an
   * untouched field dirty and, on Save, silently reverting the remote change.
   * The read-only "what it does today" panel deliberately keeps reading the
   * live row; only the comparison is frozen.
   */
  const [baseline] = useState(schedule)
  const isScript = baseline.schedule_type === "script_trigger"

  const [name, setName] = useState(schedule.name)
  const [timingInput, setTimingInput] = useState("")
  const [prompt, setPrompt] = useState(schedule.prompt ?? "")
  const [command, setCommand] = useState(schedule.command ?? "")
  const [generated, setGenerated] = useState<GeneratedTiming | null>(null)
  const [generateError, setGenerateError] = useState<string | null>(null)

  const generateMutation = useMutation({
    mutationFn: (naturalLanguage: string) =>
      AgentsService.generateSchedule({
        id: agentId,
        requestBody: {
          natural_language: naturalLanguage,
          timezone: userTimezone,
          schedule_type: baseline.schedule_type,
        },
      }),
  })

  const updateMutation = useMutation({
    mutationFn: (body: UpdateScheduleRequest) =>
      AgentsService.updateSchedule({
        id: agentId,
        scheduleId: schedule.id,
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast("Schedule updated")
      queryClient.invalidateQueries({ queryKey: ["agent-schedules", agentId] })
      onClose()
    },
    onError: (err: unknown) =>
      showErrorToast(getErrorMessage(err, "Failed to update schedule")),
  })

  const handleGenerate = async () => {
    if (!timingInput.trim()) return
    setGenerateError(null)
    setGenerated(null)
    try {
      const result = await generateMutation.mutateAsync(timingInput)
      if (result.success && result.cron_string && result.description) {
        setGenerated({
          description: result.description,
          cron_string: result.cron_string,
          next_execution: result.next_execution ?? "",
        })
      } else {
        setGenerateError(result.error || "Failed to generate schedule")
      }
    } catch (err: unknown) {
      setGenerateError(getErrorMessage(err, "Failed to generate schedule"))
    }
  }

  // Only what actually changed is sent: the backend treats every present key
  // as an update, and re-sending an unchanged cron would recompute the next
  // run for no reason. Compared trimmed and sent trimmed, so that adding a
  // trailing space is not a change and cannot fire a no-op PUT that then
  // toasts success.
  const trimmedName = name.trim()
  const changes: UpdateScheduleRequest = {}
  if (trimmedName !== baseline.name.trim()) changes.name = trimmedName
  if (isScript) {
    const trimmedCommand = command.trim()
    if (trimmedCommand !== (baseline.command ?? "").trim()) {
      changes.command = trimmedCommand || null
    }
  } else {
    const trimmedPrompt = prompt.trim()
    if (trimmedPrompt !== (baseline.prompt ?? "").trim()) {
      changes.prompt = trimmedPrompt || null
    }
  }
  if (generated) {
    changes.cron_string = generated.cron_string
    changes.timezone = userTimezone
    changes.description = generated.description
  }

  const isDirty = Object.keys(changes).length > 0
  const isBusy = updateMutation.isPending || generateMutation.isPending

  return (
    <Dialog
      open={open}
      // Escape and outside clicks must not close the dialog mid-request: the
      // pending state on Save would leave with it.
      onOpenChange={(next) => {
        if (!next && !updateMutation.isPending) onClose()
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit schedule</DialogTitle>
          <DialogDescription>
            Update the name, timing or {isScript ? "command" : "prompt"} for{" "}
            <strong>{baseline.name}</strong>.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="edit-schedule-name">Name</Label>
            <Input
              id="edit-schedule-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="edit-schedule-timing">Timing</Label>
            {/* What it does today, until a new timing has been generated. */}
            {!generated && (
              <div className="bg-secondary p-3 rounded-md space-y-1.5">
                <div className="flex items-start gap-2">
                  <Check className="h-4 w-4 text-green-600 mt-0.5 shrink-0" />
                  <span className="font-medium text-sm">
                    {schedule.description}
                  </span>
                </div>
                {schedule.next_execution && (
                  <div className="flex items-start gap-2 text-sm text-muted-foreground">
                    <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                    <span>
                      Next: {formatNextExecution(schedule.next_execution)}
                    </span>
                  </div>
                )}
              </div>
            )}
            <div className="flex gap-2">
              <Input
                id="edit-schedule-timing"
                value={timingInput}
                onChange={(e) => setTimingInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleGenerate()}
                placeholder="Enter new timing to change the schedule…"
                disabled={isBusy}
              />
              <Button
                onClick={handleGenerate}
                disabled={!timingInput.trim() || isBusy}
                variant="outline"
              >
                {generateMutation.isPending ? (
                  "Generating…"
                ) : (
                  <>
                    <Sparkles className="h-4 w-4 mr-1" />
                    Generate
                  </>
                )}
              </Button>
            </div>
            {generated && (
              <div className="bg-secondary p-3 rounded-md space-y-1.5">
                <div className="flex items-start gap-2">
                  <AlertCircle className="h-4 w-4 text-blue-500 mt-0.5 shrink-0" />
                  <span className="font-medium text-sm">
                    New: {generated.description}
                  </span>
                </div>
                {generated.next_execution && (
                  <div className="flex items-start gap-2 text-sm text-muted-foreground">
                    <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                    <span>
                      Next: {formatNextExecution(generated.next_execution)}
                    </span>
                  </div>
                )}
              </div>
            )}
            {generateError && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertTitle>Couldn&apos;t work out that timing</AlertTitle>
                <AlertDescription>{generateError}</AlertDescription>
              </Alert>
            )}
          </div>

          {isScript ? (
            <div className="space-y-2">
              <Label htmlFor="edit-schedule-command">Command</Label>
              <Input
                id="edit-schedule-command"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                maxLength={2000}
                className="font-mono text-sm"
                placeholder="e.g., bash scripts/check_status.sh"
              />
              <p className="text-xs text-muted-foreground">
                If output is &quot;OK&quot;, no session is started. Any other
                output triggers a new agent session.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              <Label htmlFor="edit-schedule-prompt">Prompt (optional)</Label>
              <Textarea
                id="edit-schedule-prompt"
                placeholder="Leave empty to use the agent's entrypoint prompt"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={onClose}
            disabled={updateMutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={updateMutation.isPending}
            // A script trigger with no command cannot run, and the backend
            // silently drops `command: null` rather than clearing it — so an
            // emptied command would close this dialog on a success toast and
            // change nothing. Same guard the create dialog applies.
            disabled={
              !trimmedName ||
              (isScript && !command.trim()) ||
              !isDirty ||
              generateMutation.isPending
            }
            onClick={() => updateMutation.mutate(changes)}
          >
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
