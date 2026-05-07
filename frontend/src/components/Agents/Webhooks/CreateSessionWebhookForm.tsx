import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2, Info } from "lucide-react"

import type {
  AgentWebhookCreateSession,
  AgentWebhookPublicWithToken,
} from "@/client"
import { AgentWebhooksService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { WebhookTokenDisplay } from "./WebhookTokenDisplay"

interface CreateSessionWebhookFormProps {
  agentId: string
  onClose: () => void
  onBack: () => void
}

export function CreateSessionWebhookForm({
  agentId,
  onClose,
  onBack,
}: CreateSessionWebhookFormProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [name, setName] = useState("")
  const [sessionMode, setSessionMode] =
    useState<"conversation" | "building">("conversation")
  const [prompt, setPrompt] = useState("")
  const [payloadTemplate, setPayloadTemplate] = useState("")
  const [created, setCreated] = useState<AgentWebhookPublicWithToken | null>(
    null,
  )

  const createMutation = useMutation({
    mutationFn: (body: AgentWebhookCreateSession) =>
      AgentWebhooksService.createSessionWebhook({
        agentId,
        requestBody: body,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["agent-webhooks", agentId] })
      setCreated(result)
      showSuccessToast("Session webhook created")
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to create session webhook",
      )
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    createMutation.mutate({
      name: name.trim(),
      type: "session",
      session_mode: sessionMode,
      prompt: prompt.trim() ? prompt.trim() : null,
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
          placeholder="e.g., GitHub push handler"
          maxLength={255}
          required
        />
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-1.5">
          <Label htmlFor="webhook-mode">Session mode</Label>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Info className="h-3.5 w-3.5 text-muted-foreground" />
              </TooltipTrigger>
              <TooltipContent side="right" className="text-xs max-w-xs">
                Conversation mode runs the agent&apos;s production prompt.
                Building mode is for editing scripts / prompts and uses a
                larger context window.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
        <Select
          value={sessionMode}
          onValueChange={(v) =>
            setSessionMode(v as "conversation" | "building")
          }
        >
          <SelectTrigger id="webhook-mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="conversation">Conversation</SelectItem>
            <SelectItem value="building">Building</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-prompt">Prompt (optional)</Label>
        <Textarea
          id="webhook-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Leave empty to use the agent's entrypoint prompt"
          rows={3}
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="webhook-payload-template">
          Payload template (optional)
        </Label>
        <Textarea
          id="webhook-payload-template"
          value={payloadTemplate}
          onChange={(e) => setPayloadTemplate(e.target.value)}
          placeholder="Static context prepended to the incoming webhook payload"
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
        <Button type="submit" disabled={!name.trim() || createMutation.isPending}>
          {createMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
          ) : null}
          Create webhook
        </Button>
      </div>
    </form>
  )
}
