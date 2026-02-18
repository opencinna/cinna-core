import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, AlertCircle, Loader2, Mail, RefreshCw } from "lucide-react"
import { EmailIntegrationService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useCustomToast from "@/hooks/useCustomToast"
import { EmailAccessModal } from "./EmailAccessModal"
import { EmailSessionsModal } from "./EmailSessionsModal"
import { EmailConnectionModal } from "./EmailConnectionModal"

interface EmailIntegrationCardProps {
  agentId: string
}

export function EmailIntegrationCard({ agentId }: EmailIntegrationCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Modal state
  const [accessModalOpen, setAccessModalOpen] = useState(false)
  const [sessionsModalOpen, setSessionsModalOpen] = useState(false)
  const [connectionModalOpen, setConnectionModalOpen] = useState(false)

  // Local enabled state for when no integration exists yet
  const [localEnabled, setLocalEnabled] = useState(false)

  // Fetch current integration config
  const { data: integration, isLoading } = useQuery({
    queryKey: ["email-integration", agentId],
    queryFn: () => EmailIntegrationService.getEmailIntegration({ agentId }),
  })

  // Enable/disable mutations
  const enableMutation = useMutation({
    mutationFn: () =>
      EmailIntegrationService.enableEmailIntegration({ agentId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["email-integration", agentId] })
      showSuccessToast("Email integration enabled")
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to enable email integration")
    },
  })

  const disableMutation = useMutation({
    mutationFn: () =>
      EmailIntegrationService.disableEmailIntegration({ agentId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["email-integration", agentId] })
      showSuccessToast("Email integration disabled")
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to disable email integration")
    },
  })

  // Process emails mutation
  const processEmailsMutation = useMutation({
    mutationFn: () =>
      EmailIntegrationService.processEmails({ agentId }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["email-integration", agentId] })
      showSuccessToast(data.message || "Emails processed")
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to process emails")
    },
  })

  const handleToggleEnabled = () => {
    if (!integration) {
      setLocalEnabled((prev) => !prev)
      return
    }
    if (integration.enabled) {
      disableMutation.mutate()
    } else {
      enableMutation.mutate()
    }
  }

  const isToggling = enableMutation.isPending || disableMutation.isPending
  const isEnabled = integration ? integration.enabled : localEnabled

  // Validation
  const connectionValid = !!(
    integration?.incoming_server_id &&
    integration?.incoming_mailbox &&
    integration?.outgoing_server_id &&
    integration?.outgoing_from_address
  )
  const accessValid =
    integration?.access_mode === "open" ||
    !!(integration?.auto_approve_email_pattern || integration?.allowed_domains)
  const configComplete = connectionValid && accessValid

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-6">
          <div className="flex items-center justify-center text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin mr-2" />
            Loading email integration...
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle>Email Integration</CardTitle>
            <CardDescription>
              Receive and respond to emails as agent sessions
            </CardDescription>
          </div>
          <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
            <div className="relative">
              <input
                type="checkbox"
                checked={isEnabled}
                onChange={handleToggleEnabled}
                disabled={isToggling}
                className="sr-only"
              />
              <div
                className={`block h-6 w-11 rounded-full transition-colors ${
                  isEnabled ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                }`}
              ></div>
              <div
                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  isEnabled ? "translate-x-5" : ""
                }`}
              ></div>
            </div>
          </label>
        </div>
      </CardHeader>
      <CardContent>
        {isEnabled && (
          <div className="space-y-4">
            {/* Clone count display */}
            {integration && (integration.agent_session_mode === "clone") && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Mail className="h-4 w-4" />
                <span>{integration.email_clone_count}/{integration.max_clones ?? 50} email clones active</span>
              </div>
            )}

            {/* Config buttons */}
            <div className="flex gap-2 flex-wrap">
              <Button
                variant="outline"
                onClick={() => setAccessModalOpen(true)}
              >
                Access
              </Button>
              <Button
                variant="outline"
                onClick={() => setSessionsModalOpen(true)}
              >
                Sessions
              </Button>
              <Button
                variant="outline"
                onClick={() => setConnectionModalOpen(true)}
              >
                Connection
              </Button>
              <Button
                onClick={() => processEmailsMutation.mutate()}
                disabled={!configComplete || processEmailsMutation.isPending}
              >
                {processEmailsMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                ) : (
                  <RefreshCw className="h-4 w-4 mr-2" />
                )}
                Process Emails
              </Button>
            </div>

            {/* Validation status */}
            {integration ? (
              configComplete ? (
                <div className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  <span>Configuration complete</span>
                </div>
              ) : (
                <div className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                  <AlertCircle className="h-3.5 w-3.5" />
                  <span>
                    Missing: {[
                      !connectionValid && "connection",
                      !accessValid && "access rules",
                    ].filter(Boolean).join(", ")}
                  </span>
                </div>
              )
            ) : (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <AlertCircle className="h-3.5 w-3.5" />
                <span>Not configured yet</span>
              </div>
            )}

          </div>
        )}
      </CardContent>

      {/* Modals */}
      <EmailAccessModal
        agentId={agentId}
        integration={integration}
        open={accessModalOpen}
        onClose={() => setAccessModalOpen(false)}
      />
      <EmailSessionsModal
        agentId={agentId}
        integration={integration}
        open={sessionsModalOpen}
        onClose={() => setSessionsModalOpen(false)}
      />
      <EmailConnectionModal
        agentId={agentId}
        integration={integration}
        open={connectionModalOpen}
        onClose={() => setConnectionModalOpen(false)}
      />
    </Card>
  )
}
