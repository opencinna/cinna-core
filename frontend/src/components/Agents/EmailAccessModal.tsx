import { useState, useEffect } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"

import { CheckCircle2, AlertCircle } from "lucide-react"
import {
  EmailIntegrationService,
  type AgentEmailIntegrationCreate,
  type AgentEmailIntegrationPublic,
  type EmailAccessMode,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"

interface EmailAccessModalProps {
  agentId: string
  integration: AgentEmailIntegrationPublic | null | undefined
  open: boolean
  onClose: () => void
}

export function EmailAccessModal({
  agentId,
  integration,
  open,
  onClose,
}: EmailAccessModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [accessMode, setAccessMode] = useState<EmailAccessMode>("restricted")
  const [autoApprovePattern, setAutoApprovePattern] = useState("")
  const [allowedDomains, setAllowedDomains] = useState("")

  useEffect(() => {
    if (open && integration) {
      setAccessMode(integration.access_mode || "restricted")
      setAutoApprovePattern(integration.auto_approve_email_pattern || "")
      setAllowedDomains(integration.allowed_domains || "")
    }
  }, [open, integration])

  const saveMutation = useMutation({
    mutationFn: (data: AgentEmailIntegrationCreate) =>
      EmailIntegrationService.createOrUpdateEmailIntegration({
        agentId,
        requestBody: data,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["email-integration", agentId] })
      showSuccessToast("Access settings saved")
      onClose()
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to save access settings")
    },
  })

  const handleSave = () => {
    saveMutation.mutate({
      enabled: integration?.enabled ?? true,
      access_mode: accessMode,
      auto_approve_email_pattern: autoApprovePattern || null,
      allowed_domains: allowedDomains || null,
      max_clones: integration?.max_clones ?? 50,
      clone_share_mode: integration?.clone_share_mode || "user",
      agent_session_mode: integration?.agent_session_mode || "clone",
      incoming_server_id: integration?.incoming_server_id || null,
      incoming_mailbox: integration?.incoming_mailbox || null,
      outgoing_server_id: integration?.outgoing_server_id || null,
      outgoing_from_address: integration?.outgoing_from_address || null,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Email Access Settings</DialogTitle>
          <DialogDescription>
            Control who can interact with this agent via email
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-2">
          {/* Access Mode */}
          <div className="space-y-3">
            <Label className="text-sm font-medium">Access Mode</Label>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="modal_access_mode"
                  value="restricted"
                  checked={accessMode === "restricted"}
                  onChange={() => setAccessMode("restricted")}
                  className="accent-primary"
                />
                <span className="text-sm">Restricted</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="modal_access_mode"
                  value="open"
                  checked={accessMode === "open"}
                  onChange={() => setAccessMode("open")}
                  className="accent-primary"
                />
                <span className="text-sm">Open</span>
              </label>
            </div>
            <p className="text-xs text-muted-foreground">
              {accessMode === "restricted"
                ? "Only pre-shared users or matching email patterns can interact"
                : "Any email sender can interact (subject to domain restrictions)"}
            </p>
          </div>

          {/* Auto-approve pattern (restricted mode only) */}
          {accessMode === "restricted" && (
            <div className="space-y-2">
              <Label htmlFor="modal_auto_approve_pattern">Auto-Approve Email Pattern</Label>
              <Input
                id="modal_auto_approve_pattern"
                placeholder="*@example.com, tech-*@another.com"
                value={autoApprovePattern}
                onChange={(e) => setAutoApprovePattern(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Comma-separated glob patterns. Matching senders are auto-approved.
              </p>
            </div>
          )}

          {/* Domain allowlist */}
          <div className="space-y-2">
            <Label htmlFor="modal_allowed_domains">Allowed Domains</Label>
            <Input
              id="modal_allowed_domains"
              placeholder="example.com, partner.org"
              value={allowedDomains}
              onChange={(e) => setAllowedDomains(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Comma-separated. Only emails from these domains can trigger conversations. Leave empty for no restriction.
            </p>
          </div>
        </div>

        {/* Validation */}
        {accessMode === "open" || autoApprovePattern || allowedDomains ? (
          <div className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="h-3.5 w-3.5" />
            <span>Access settings valid</span>
          </div>
        ) : (
          <div className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <AlertCircle className="h-3.5 w-3.5" />
            <span>Restricted mode requires an email pattern or allowed domains</span>
          </div>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <LoadingButton onClick={handleSave} loading={saveMutation.isPending}>
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
