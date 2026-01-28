import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { TaskTriggersApi } from "./triggerApi"
import type { TaskTriggerPublicWithToken } from "./triggerApi"
import { WebhookTokenDisplay } from "./WebhookTokenDisplay"
import useCustomToast from "@/hooks/useCustomToast"

interface AddWebhookTriggerFormProps {
  taskId: string
  onClose: () => void
}

export function AddWebhookTriggerForm({ taskId, onClose }: AddWebhookTriggerFormProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [name, setName] = useState("")
  const [payloadTemplate, setPayloadTemplate] = useState("")
  const [createdTrigger, setCreatedTrigger] = useState<TaskTriggerPublicWithToken | null>(null)

  const createMutation = useMutation({
    mutationFn: () =>
      TaskTriggersApi.createWebhookTrigger(taskId, {
        name,
        type: "webhook",
        payload_template: payloadTemplate || null,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["task-triggers", taskId] })
      setCreatedTrigger(result)
      showSuccessToast("Webhook trigger created")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to create webhook trigger")
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    createMutation.mutate()
  }

  // After creation, show the token display
  if (createdTrigger && createdTrigger.webhook_token && createdTrigger.webhook_url) {
    return (
      <div className="space-y-4">
        <WebhookTokenDisplay
          token={createdTrigger.webhook_token}
          webhookUrl={createdTrigger.webhook_url}
        />
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </div>
    )
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="webhook-name">Name</Label>
        <Input
          id="webhook-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g., GitHub PR webhook"
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-payload">Payload Template (optional)</Label>
        <Textarea
          id="webhook-payload"
          value={payloadTemplate}
          onChange={(e) => setPayloadTemplate(e.target.value)}
          placeholder="Review the following GitHub event:"
          rows={3}
        />
        <p className="text-xs text-muted-foreground">
          Static context prepended to the webhook request body. The incoming webhook payload will be appended after this template.
        </p>
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={!name.trim() || createMutation.isPending}>
          {createMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
          ) : null}
          Create Webhook
        </Button>
      </div>
    </form>
  )
}
