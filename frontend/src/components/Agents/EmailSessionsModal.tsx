import { useState, useEffect } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"

import {
  EmailIntegrationService,
  type AgentEmailIntegrationCreate,
  type AgentEmailIntegrationPublic,
  type AgentSessionMode,
  type EmailCloneShareMode,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"

interface EmailSessionsModalProps {
  agentId: string
  integration: AgentEmailIntegrationPublic | null | undefined
  open: boolean
  onClose: () => void
}

export function EmailSessionsModal({
  agentId,
  integration,
  open,
  onClose,
}: EmailSessionsModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [sessionMode, setSessionMode] = useState<AgentSessionMode>("clone")
  const [maxClones, setMaxClones] = useState(50)
  const [cloneShareMode, setCloneShareMode] = useState<EmailCloneShareMode>("user")

  useEffect(() => {
    if (open && integration) {
      setSessionMode(integration.agent_session_mode || "clone")
      setMaxClones(integration.max_clones ?? 50)
      setCloneShareMode(integration.clone_share_mode || "user")
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
      showSuccessToast("Session settings saved")
      onClose()
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to save session settings")
    },
  })

  const handleSave = () => {
    saveMutation.mutate({
      enabled: integration?.enabled ?? true,
      access_mode: integration?.access_mode || "restricted",
      auto_approve_email_pattern: integration?.auto_approve_email_pattern || null,
      allowed_domains: integration?.allowed_domains || null,
      max_clones: maxClones,
      clone_share_mode: cloneShareMode,
      agent_session_mode: sessionMode,
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
          <DialogTitle>Email Session Settings</DialogTitle>
          <DialogDescription>
            Configure how email interactions create agent sessions
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-2">
          {/* Session Mode */}
          <div className="space-y-3">
            <Label className="text-sm font-medium">Session Mode</Label>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="modal_session_mode"
                  value="clone"
                  checked={sessionMode === "clone"}
                  onChange={() => setSessionMode("clone")}
                  className="accent-primary"
                />
                <span className="text-sm">Clone</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="modal_session_mode"
                  value="owner"
                  checked={sessionMode === "owner"}
                  onChange={() => setSessionMode("owner")}
                  className="accent-primary"
                />
                <span className="text-sm">Owner</span>
              </label>
            </div>
            <p className="text-xs text-muted-foreground">
              {sessionMode === "owner"
                ? "Emails create sessions on this agent directly — replies run in your own environment. Ideal for personal automation."
                : "Each sender gets their own isolated clone with a separate environment and session history."}
            </p>
          </div>

          {/* Max clones & clone share mode (clone mode only) */}
          {sessionMode === "clone" && (
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="modal_max_clones">Max Clones</Label>
                <Input
                  id="modal_max_clones"
                  type="number"
                  min={1}
                  max={1000}
                  value={maxClones}
                  onChange={(e) => setMaxClones(parseInt(e.target.value) || 50)}
                />
              </div>
              <div className="space-y-2">
                <Label>Clone Share Mode</Label>
                <Select
                  value={cloneShareMode}
                  onValueChange={(v) => setCloneShareMode(v as EmailCloneShareMode)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="user">User</SelectItem>
                    <SelectItem value="builder">Builder</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          )}

          {/* Clone count display */}
          {integration && sessionMode === "clone" && (
            <p className="text-sm text-muted-foreground">
              Currently {integration.email_clone_count}/{integration.max_clones ?? 50} email clones active
            </p>
          )}
        </div>

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
