import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"

import type {
  AgentWebhookCreateScript,
  AgentWebhookPublicWithToken,
} from "@/client"
import { AgentWebhooksService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { WebhookTokenDisplay } from "./WebhookTokenDisplay"

interface CreateScriptWebhookFormProps {
  agentId: string
  onClose: () => void
  onBack: () => void
}

export function CreateScriptWebhookForm({
  agentId,
  onClose,
  onBack,
}: CreateScriptWebhookFormProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [name, setName] = useState("")
  const [command, setCommand] = useState("")
  const [timeoutSeconds, setTimeoutSeconds] = useState<number>(120)
  const [payloadTemplate, setPayloadTemplate] = useState("")
  const [created, setCreated] = useState<AgentWebhookPublicWithToken | null>(
    null,
  )

  const createMutation = useMutation({
    mutationFn: (body: AgentWebhookCreateScript) =>
      AgentWebhooksService.createScriptWebhook({
        agentId,
        requestBody: body,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["agent-webhooks", agentId] })
      setCreated(result)
      showSuccessToast("Script webhook created")
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to create script webhook",
      )
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !command.trim()) return
    createMutation.mutate({
      name: name.trim(),
      type: "script",
      command: command.trim(),
      command_timeout_seconds: timeoutSeconds,
      payload_template: payloadTemplate.trim()
        ? payloadTemplate.trim()
        : null,
    })
  }

  if (created && created.webhook_url) {
    return (
      <div className="space-y-4">
        <WebhookTokenDisplay
          token={created.webhook_token}
          webhookUrl={created.webhook_url}
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
          placeholder="e.g., GitHub push -> sync"
          maxLength={255}
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-command">Command</Label>
        <Textarea
          id="webhook-command"
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          placeholder='e.g., bash scripts/handle_webhook.sh "$WEBHOOK_PAYLOAD"'
          rows={3}
          maxLength={2000}
          className="font-mono text-sm"
          required
        />
        <p className="text-xs text-muted-foreground">
          Use <code>$WEBHOOK_PAYLOAD</code>, <code>$WEBHOOK_NAME</code>,{" "}
          <code>$WEBHOOK_HEADERS_JSON</code>, <code>$WEBHOOK_ID</code>, and{" "}
          <code>$WEBHOOK_CONTENT_TYPE</code> inside your command. The raw
          request body is also piped to stdin.
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-timeout">Timeout (seconds)</Label>
        <Input
          id="webhook-timeout"
          type="number"
          min={1}
          max={300}
          value={timeoutSeconds}
          onChange={(e) => {
            const v = Number(e.target.value)
            if (Number.isFinite(v)) setTimeoutSeconds(v)
          }}
        />
        <p className="text-xs text-muted-foreground">
          Maximum 300 seconds. The command is killed if it exceeds this limit.
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-payload-template">
          Payload template (optional)
        </Label>
        <Textarea
          id="webhook-payload-template"
          value={payloadTemplate}
          onChange={(e) => setPayloadTemplate(e.target.value)}
          placeholder="Static context — used by the backend (e.g. logged with the call) but not auto-injected into the shell"
          rows={3}
          maxLength={10000}
        />
        <p className="text-xs text-muted-foreground">
          Incoming webhook bodies are forwarded up to 10 KB (truncated
          beyond). Total request size capped at 64 KB.
        </p>
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button
          type="submit"
          disabled={
            !name.trim() || !command.trim() || createMutation.isPending
          }
        >
          {createMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
          ) : null}
          Create webhook
        </Button>
      </div>
    </form>
  )
}
