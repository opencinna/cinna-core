import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, Webhook } from "lucide-react"

import type {
  AgentWebhookPublic,
  AgentWebhookPublicWithToken,
} from "@/client"
import { AgentWebhooksService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { CreateScriptWebhookForm } from "./CreateScriptWebhookForm"
import { CreateSessionWebhookForm } from "./CreateSessionWebhookForm"
import { EditWebhookDialog } from "./EditWebhookDialog"
import { WebhookCard } from "./WebhookCard"
import { WebhookLogsModal } from "./WebhookLogsModal"
import {
  WebhookTypeSelectorDialog,
  type WebhookType,
} from "./WebhookTypeSelectorDialog"
import { WebhookTokenDisplay } from "./WebhookTokenDisplay"

interface AgentWebhooksCardProps {
  agentId: string
}

type CreateStep = "type_select" | "session_form" | "script_form"

export function AgentWebhooksCard({ agentId }: AgentWebhooksCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [createStep, setCreateStep] = useState<CreateStep | null>(null)

  const [editingWebhook, setEditingWebhook] =
    useState<AgentWebhookPublic | null>(null)
  const [editOpen, setEditOpen] = useState(false)

  const [logsWebhook, setLogsWebhook] = useState<AgentWebhookPublic | null>(
    null,
  )
  const [logsOpen, setLogsOpen] = useState(false)

  const [regenerated, setRegenerated] =
    useState<AgentWebhookPublicWithToken | null>(null)

  const queryKey = ["agent-webhooks", agentId] as const

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => AgentWebhooksService.listWebhooks({ agentId }),
    enabled: !!agentId,
  })

  const webhooks = data?.data ?? []

  const toggleMutation = useMutation({
    mutationFn: ({
      webhook,
      enabled,
    }: {
      webhook: AgentWebhookPublic
      enabled: boolean
    }) =>
      AgentWebhooksService.updateWebhook({
        agentId,
        webhookPk: webhook.id,
        requestBody: { enabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey })
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to toggle webhook",
      )
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (webhook: AgentWebhookPublic) =>
      AgentWebhooksService.deleteWebhook({
        agentId,
        webhookPk: webhook.id,
      }),
    onSuccess: (_data, webhook) => {
      showSuccessToast("Webhook deleted")
      queryClient.invalidateQueries({ queryKey })
      queryClient.invalidateQueries({
        queryKey: ["agent-webhook-logs", webhook.id],
      })
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to delete webhook",
      )
    },
  })

  const regenerateMutation = useMutation({
    mutationFn: (webhook: AgentWebhookPublic) =>
      AgentWebhooksService.regenerateToken({
        agentId,
        webhookPk: webhook.id,
      }),
    onSuccess: (result, webhook) => {
      showSuccessToast("Webhook token regenerated")
      setRegenerated(result)
      queryClient.invalidateQueries({ queryKey })
      queryClient.invalidateQueries({
        queryKey: ["agent-webhook-logs", webhook.id],
      })
    },
    onError: (error) => {
      showErrorToast(
        (error as Error).message || "Failed to regenerate token",
      )
    },
  })

  const handleCreateOpen = () => setCreateStep("type_select")
  const handleCreateClose = () => setCreateStep(null)

  const handleTypeSelect = (type: WebhookType) => {
    setCreateStep(type === "session" ? "session_form" : "script_form")
  }

  const handleEdit = (webhook: AgentWebhookPublic) => {
    setEditingWebhook(webhook)
    setEditOpen(true)
  }

  const handleShowLogs = (webhook: AgentWebhookPublic) => {
    setLogsWebhook(webhook)
    setLogsOpen(true)
    // Refresh the parent list so 'Fired X ago' picks up activity that
    // happened while the user was elsewhere.
    queryClient.invalidateQueries({ queryKey })
  }

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <Webhook className="h-5 w-5" />
                Webhooks
              </CardTitle>
              <CardDescription>
                Trigger this agent on demand from external systems via
                authenticated HTTP webhooks.
              </CardDescription>
            </div>
            <Button size="sm" onClick={handleCreateOpen}>
              <Plus className="h-4 w-4 mr-1" />
              Webhook
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : webhooks.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No webhooks yet. Use webhooks to trigger this agent from
              external systems like GitHub, Zapier, or your own scripts.
              Choose Session Trigger to start a conversation or Script
              Trigger to run a command.
            </p>
          ) : (
            <div className="space-y-1.5">
              {webhooks.map((webhook) => (
                <WebhookCard
                  key={webhook.id}
                  webhook={webhook}
                  onEdit={handleEdit}
                  onShowLogs={handleShowLogs}
                  onToggleEnabled={(w) =>
                    toggleMutation.mutate({
                      webhook: w,
                      enabled: !w.enabled,
                    })
                  }
                  onRegenerateToken={(w) => regenerateMutation.mutate(w)}
                  onDelete={(w) => deleteMutation.mutate(w)}
                  pendingAction={
                    toggleMutation.isPending &&
                    toggleMutation.variables?.webhook.id === webhook.id
                  }
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Type selector */}
      <WebhookTypeSelectorDialog
        open={createStep === "type_select"}
        onOpenChange={(open) => {
          if (!open) handleCreateClose()
        }}
        onSelect={handleTypeSelect}
      />

      {/* Session create form */}
      <Dialog
        open={createStep === "session_form"}
        onOpenChange={(open) => {
          if (!open) handleCreateClose()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Session Webhook</DialogTitle>
            <DialogDescription>
              Each call starts a new agent session seeded with the incoming
              payload.
            </DialogDescription>
          </DialogHeader>
          <CreateSessionWebhookForm
            agentId={agentId}
            onClose={handleCreateClose}
            onBack={() => setCreateStep("type_select")}
          />
        </DialogContent>
      </Dialog>

      {/* Script create form */}
      <Dialog
        open={createStep === "script_form"}
        onOpenChange={(open) => {
          if (!open) handleCreateClose()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Script Webhook</DialogTitle>
            <DialogDescription>
              Each call runs a shell command in the agent&apos;s environment.
            </DialogDescription>
          </DialogHeader>
          <CreateScriptWebhookForm
            agentId={agentId}
            onClose={handleCreateClose}
            onBack={() => setCreateStep("type_select")}
          />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <EditWebhookDialog
        agentId={agentId}
        webhook={editingWebhook}
        open={editOpen}
        onOpenChange={(open) => {
          setEditOpen(open)
          if (!open) setEditingWebhook(null)
        }}
      />

      {/* Logs modal */}
      <WebhookLogsModal
        agentId={agentId}
        webhook={logsWebhook}
        open={logsOpen}
        onOpenChange={(open) => {
          setLogsOpen(open)
          if (!open) setLogsWebhook(null)
        }}
      />

      {/* Token reveal after regenerate */}
      <Dialog
        open={!!regenerated}
        onOpenChange={(open) => {
          if (!open) setRegenerated(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New Webhook Token</DialogTitle>
            <DialogDescription>
              Copy the new token now — it will not be shown again. The old
              token has already stopped working.
            </DialogDescription>
          </DialogHeader>
          {regenerated && regenerated.webhook_url && (
            <WebhookTokenDisplay
              token={regenerated.webhook_token}
              webhookUrl={regenerated.webhook_url}
            />
          )}
          <div className="flex justify-end">
            <Button onClick={() => setRegenerated(null)}>Done</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
