import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2, Info } from "lucide-react"

import type { AgentWebhookPublic, AgentWebhookUpdate } from "@/client"
import { AgentWebhooksService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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

interface EditWebhookDialogProps {
  agentId: string
  webhook: AgentWebhookPublic | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EditWebhookDialog({
  agentId,
  webhook,
  open,
  onOpenChange,
}: EditWebhookDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [name, setName] = useState("")
  const [sessionMode, setSessionMode] =
    useState<"conversation" | "building">("conversation")
  const [prompt, setPrompt] = useState("")
  const [command, setCommand] = useState("")
  const [timeoutSeconds, setTimeoutSeconds] = useState<number>(120)
  const [payloadTemplate, setPayloadTemplate] = useState("")

  // Reset form when a different webhook is loaded.
  useEffect(() => {
    if (!webhook) return
    setName(webhook.name)
    setPayloadTemplate(webhook.payload_template ?? "")
    if (webhook.type === "session") {
      setSessionMode(
        webhook.session_mode === "building" ? "building" : "conversation",
      )
      setPrompt(webhook.prompt ?? "")
      setCommand("")
      setTimeoutSeconds(120)
    } else {
      setCommand(webhook.command ?? "")
      setTimeoutSeconds(webhook.command_timeout_seconds ?? 120)
      setPrompt("")
      setSessionMode("conversation")
    }
  }, [webhook])

  const updateMutation = useMutation({
    mutationFn: (body: AgentWebhookUpdate) =>
      AgentWebhooksService.updateWebhook({
        agentId,
        webhookPk: webhook!.id,
        requestBody: body,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-webhooks", agentId] })
      showSuccessToast("Webhook updated")
      onOpenChange(false)
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to update webhook",
      )
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!webhook || !name.trim()) return

    const body: AgentWebhookUpdate = {}
    if (name !== webhook.name) body.name = name.trim()
    const newPayloadTemplate = payloadTemplate.trim() || null
    if (newPayloadTemplate !== (webhook.payload_template ?? null)) {
      body.payload_template = newPayloadTemplate
    }

    if (webhook.type === "session") {
      const newPrompt = prompt.trim() || null
      if (newPrompt !== (webhook.prompt ?? null)) {
        body.prompt = newPrompt
      }
      if (sessionMode !== (webhook.session_mode ?? "conversation")) {
        body.session_mode = sessionMode
      }
    } else if (webhook.type === "script") {
      if (!command.trim()) {
        showErrorToast("Command cannot be empty")
        return
      }
      if (command.trim() !== (webhook.command ?? "")) {
        body.command = command.trim()
      }
      if (timeoutSeconds !== (webhook.command_timeout_seconds ?? 120)) {
        body.command_timeout_seconds = timeoutSeconds
      }
    }

    if (Object.keys(body).length === 0) {
      onOpenChange(false)
      return
    }

    updateMutation.mutate(body)
  }

  if (!webhook) return null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Edit {webhook.type === "session" ? "Session" : "Script"} Webhook
          </DialogTitle>
          <DialogDescription>
            The webhook type cannot be changed. Regenerate the token from the
            list if you need a new bearer secret.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="edit-webhook-name">Name</Label>
            <Input
              id="edit-webhook-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={255}
              required
            />
          </div>

          {webhook.type === "session" ? (
            <>
              <div className="space-y-2">
                <div className="flex items-center gap-1.5">
                  <Label htmlFor="edit-webhook-mode">Session mode</Label>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Info className="h-3.5 w-3.5 text-muted-foreground" />
                      </TooltipTrigger>
                      <TooltipContent
                        side="right"
                        className="text-xs max-w-xs"
                      >
                        Conversation mode runs the agent&apos;s production
                        prompt. Building mode is for editing scripts /
                        prompts and uses a larger context window.
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
                  <SelectTrigger id="edit-webhook-mode">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="conversation">Conversation</SelectItem>
                    <SelectItem value="building">Building</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label htmlFor="edit-webhook-prompt">Prompt (optional)</Label>
                <Textarea
                  id="edit-webhook-prompt"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Leave empty to use the agent's entrypoint prompt"
                  rows={3}
                />
              </div>
            </>
          ) : (
            <>
              <div className="space-y-2">
                <Label htmlFor="edit-webhook-command">Command</Label>
                <Textarea
                  id="edit-webhook-command"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  rows={3}
                  maxLength={2000}
                  className="font-mono text-sm"
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Use <code>$WEBHOOK_PAYLOAD</code>,{" "}
                  <code>$WEBHOOK_HEADERS_JSON</code>, etc. The raw request
                  body is also piped to stdin.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="edit-webhook-timeout">
                  Timeout (seconds)
                </Label>
                <Input
                  id="edit-webhook-timeout"
                  type="number"
                  min={1}
                  max={300}
                  value={timeoutSeconds}
                  onChange={(e) => {
                    const v = Number(e.target.value)
                    if (Number.isFinite(v)) setTimeoutSeconds(v)
                  }}
                />
              </div>
            </>
          )}

          <div className="space-y-2">
            <Label htmlFor="edit-webhook-payload-template">
              Payload template (optional)
            </Label>
            <Textarea
              id="edit-webhook-payload-template"
              value={payloadTemplate}
              onChange={(e) => setPayloadTemplate(e.target.value)}
              rows={3}
              maxLength={10000}
            />
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || updateMutation.isPending}
            >
              {updateMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
              ) : null}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
