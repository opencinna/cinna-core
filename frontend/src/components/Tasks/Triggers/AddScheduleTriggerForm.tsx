import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { TaskTriggersApi } from "./triggerApi"
import useCustomToast from "@/hooks/useCustomToast"

interface AddScheduleTriggerFormProps {
  taskId: string
  onClose: () => void
}

export function AddScheduleTriggerForm({ taskId, onClose }: AddScheduleTriggerFormProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [name, setName] = useState("")
  const [scheduleInput, setScheduleInput] = useState("")
  const [payloadTemplate, setPayloadTemplate] = useState("")
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  const createMutation = useMutation({
    mutationFn: () =>
      TaskTriggersApi.createScheduleTrigger(taskId, {
        name,
        type: "schedule",
        natural_language: scheduleInput,
        timezone,
        payload_template: payloadTemplate || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
      showSuccessToast("Schedule trigger created")
      onClose()
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to create schedule trigger")
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !scheduleInput.trim()) return
    createMutation.mutate()
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="schedule-name">Name</Label>
        <Input
          id="schedule-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g., Daily morning check"
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="schedule-input">Schedule</Label>
        <Input
          id="schedule-input"
          value={scheduleInput}
          onChange={(e) => setScheduleInput(e.target.value)}
          placeholder="e.g., every workday at 7 AM"
          required
        />
        <p className="text-xs text-muted-foreground">
          Describe the schedule in natural language. AI will convert it to a CRON expression.
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="schedule-timezone">Timezone</Label>
        <Input id="schedule-timezone" value={timezone} disabled className="text-muted-foreground" />
      </div>

      <div className="space-y-2">
        <Label htmlFor="schedule-payload">Payload Template (optional)</Label>
        <Textarea
          id="schedule-payload"
          value={payloadTemplate}
          onChange={(e) => setPayloadTemplate(e.target.value)}
          placeholder="Focus on error rates and latency metrics from the past 24 hours"
          rows={3}
        />
        <p className="text-xs text-muted-foreground">
          Optional context appended to the task description when this trigger fires.
        </p>
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={!name.trim() || !scheduleInput.trim() || createMutation.isPending}>
          {createMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
          ) : null}
          Create Schedule
        </Button>
      </div>
    </form>
  )
}
