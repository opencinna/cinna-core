import { useState, useEffect } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { CheckCircle2, AlertCircle } from "lucide-react"
import {
  EmailIntegrationService,
  MailServersService,
  type AgentEmailIntegrationCreate,
  type AgentEmailIntegrationPublic,
  type MailServerConfigPublic,
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

const NONE_VALUE = "__none__"

interface EmailConnectionModalProps {
  agentId: string
  integration: AgentEmailIntegrationPublic | null | undefined
  open: boolean
  onClose: () => void
}

export function EmailConnectionModal({
  agentId,
  integration,
  open,
  onClose,
}: EmailConnectionModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: imapServers } = useQuery({
    queryKey: ["mail-servers", "imap"],
    queryFn: () => MailServersService.listMailServers({ serverType: "imap" }),
  })

  const { data: smtpServers } = useQuery({
    queryKey: ["mail-servers", "smtp"],
    queryFn: () => MailServersService.listMailServers({ serverType: "smtp" }),
  })

  const [incomingServerId, setIncomingServerId] = useState<string | null>(null)
  const [incomingMailbox, setIncomingMailbox] = useState("")
  const [outgoingServerId, setOutgoingServerId] = useState<string | null>(null)
  const [outgoingFromAddress, setOutgoingFromAddress] = useState("")

  useEffect(() => {
    if (open && integration) {
      setIncomingServerId(integration.incoming_server_id || null)
      setIncomingMailbox(integration.incoming_mailbox || "")
      setOutgoingServerId(integration.outgoing_server_id || null)
      setOutgoingFromAddress(integration.outgoing_from_address || "")
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
      showSuccessToast("Connection settings saved")
      onClose()
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to save connection settings")
    },
  })

  const handleSave = () => {
    saveMutation.mutate({
      enabled: integration?.enabled ?? true,
      access_mode: integration?.access_mode || "restricted",
      auto_approve_email_pattern: integration?.auto_approve_email_pattern || null,
      allowed_domains: integration?.allowed_domains || null,
      max_clones: integration?.max_clones ?? 50,
      clone_share_mode: integration?.clone_share_mode || "user",
      agent_session_mode: integration?.agent_session_mode || "clone",
      incoming_server_id: incomingServerId,
      incoming_mailbox: incomingMailbox || null,
      outgoing_server_id: outgoingServerId,
      outgoing_from_address: outgoingFromAddress || null,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Email Connection Settings</DialogTitle>
          <DialogDescription>
            Configure incoming and outgoing mail servers
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-2">
          {/* Incoming mail config */}
          <div className="space-y-3">
            <Label className="text-sm font-medium">Incoming Mail (IMAP)</Label>
            <div className="space-y-2">
              <Label htmlFor="modal_incoming_server">IMAP Server</Label>
              <Select
                value={incomingServerId || NONE_VALUE}
                onValueChange={(v) =>
                  setIncomingServerId(v === NONE_VALUE ? null : v)
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select server" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE_VALUE}>None</SelectItem>
                  {(imapServers?.data || []).map((s: MailServerConfigPublic) => (
                    <SelectItem key={s.id} value={s.id}>
                      {s.name} ({s.host})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="modal_incoming_mailbox">Mailbox Address</Label>
              <Input
                id="modal_incoming_mailbox"
                placeholder="agent@example.com"
                value={incomingMailbox}
                onChange={(e) => setIncomingMailbox(e.target.value)}
              />
            </div>
            {(!imapServers?.data || imapServers.data.length === 0) && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                No IMAP servers configured. Add one in Settings &gt; Mail Servers first.
              </p>
            )}
          </div>

          {/* Outgoing mail config */}
          <div className="space-y-3 pt-2 border-t">
            <Label className="text-sm font-medium">Outgoing Mail (SMTP)</Label>
            <div className="space-y-2">
              <Label htmlFor="modal_outgoing_server">SMTP Server</Label>
              <Select
                value={outgoingServerId || NONE_VALUE}
                onValueChange={(v) =>
                  setOutgoingServerId(v === NONE_VALUE ? null : v)
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select server" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE_VALUE}>None</SelectItem>
                  {(smtpServers?.data || []).map((s: MailServerConfigPublic) => (
                    <SelectItem key={s.id} value={s.id}>
                      {s.name} ({s.host})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="modal_outgoing_from">From Address</Label>
              <Input
                id="modal_outgoing_from"
                placeholder="agent@example.com"
                value={outgoingFromAddress}
                onChange={(e) => setOutgoingFromAddress(e.target.value)}
              />
            </div>
            {(!smtpServers?.data || smtpServers.data.length === 0) && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                No SMTP servers configured. Add one in Settings &gt; Mail Servers first.
              </p>
            )}
          </div>
        </div>

        {/* Validation */}
        {incomingServerId && incomingMailbox && outgoingServerId && outgoingFromAddress ? (
          <div className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="h-3.5 w-3.5" />
            <span>Connection settings valid</span>
          </div>
        ) : (
          <div className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <AlertCircle className="h-3.5 w-3.5" />
            <span>
              Missing: {[
                !incomingServerId && "IMAP server",
                !incomingMailbox && "mailbox address",
                !outgoingServerId && "SMTP server",
                !outgoingFromAddress && "from address",
              ].filter(Boolean).join(", ")}
            </span>
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
