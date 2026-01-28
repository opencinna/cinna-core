import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { TaskTriggersApi } from "./triggerApi"
import useCustomToast from "@/hooks/useCustomToast"

interface AddExactDateTriggerFormProps {
  taskId: string
  onClose: () => void
}

export function AddExactDateTriggerForm({ taskId, onClose }: AddExactDateTriggerFormProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [name, setName] = useState("")
  const [executeAt, setExecuteAt] = useState("")
  const [payloadTemplate, setPayloadTemplate] = useState("")
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  const createMutation = useMutation({
    mutationFn: () =>
      TaskTriggersApi.createExactDateTrigger(taskId, {
        name,
        type: "exact_date",
        execute_at: new Date(executeAt).toISOString(),
        timezone,
        payload_template: payloadTemplate || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
      showSuccessToast("Exact date trigger created")
      onClose()
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to create trigger")
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !executeAt) return
    // Client-side future date validation
    if (new Date(executeAt) <= new Date()) {
      showErrorToast("Execution date must be in the future")
      return
    }
    createMutation.mutate()
  }

  // Min datetime for the picker: now
  const now = new Date()
  const minDateTime = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}T${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="date-name">Name</Label>
        <Input
          id="date-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g., Contract expiry reminder"
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="date-execute-at">Date and Time</Label>
        <Input
          id="date-execute-at"
          type="datetime-local"
          value={executeAt}
          onChange={(e) => setExecuteAt(e.target.value)}
          min={minDateTime}
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="date-timezone">Timezone</Label>
        <Input id="date-timezone" value={timezone} disabled className="text-muted-foreground" />
      </div>

      <div className="space-y-2">
        <Label htmlFor="date-payload">Payload Template (optional)</Label>
        <Textarea
          id="date-payload"
          value={payloadTemplate}
          onChange={(e) => setPayloadTemplate(e.target.value)}
          placeholder="Contract #1234 expires in 30 days. Research alternative providers."
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
        <Button type="submit" disabled={!name.trim() || !executeAt || createMutation.isPending}>
          {createMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
          ) : null}
          Create Trigger
        </Button>
      </div>
    </form>
  )
}
