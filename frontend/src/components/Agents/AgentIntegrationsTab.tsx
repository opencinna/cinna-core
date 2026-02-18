import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Copy, Check } from "lucide-react"
import { useState } from "react"

import type { AgentPublic } from "@/client"
import { AgentsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { AccessTokensCard } from "./AccessTokensCard"
import { EmailIntegrationCard } from "./EmailIntegrationCard"

interface AgentIntegrationsTabProps {
  agent: AgentPublic
}

export function AgentIntegrationsTab({ agent }: AgentIntegrationsTabProps) {
  const [copiedUrl, setCopiedUrl] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // A2A state
  const a2aConfig = agent.a2a_config as { enabled?: boolean; [key: string]: unknown } | null | undefined
  const a2aEnabled = a2aConfig?.enabled ?? false
  const a2aUrl = `${import.meta.env.VITE_API_URL}/api/v1/a2a/${agent.id}/`

  const updateA2aConfigMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agent.id,
        requestBody: {
          a2a_config: {
            ...(a2aConfig ?? {}),
            enabled: enabled
          }
        }
      }),
    onSuccess: () => {
      showSuccessToast("A2A integration updated successfully")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update A2A integration")
    },
  })

  const handleA2aEnabledChange = (checked: boolean) => {
    updateA2aConfigMutation.mutate(checked)
  }

  const handleCopyA2aUrl = async () => {
    try {
      await navigator.clipboard.writeText(a2aUrl)
      setCopiedUrl(true)
      setTimeout(() => setCopiedUrl(false), 2000)
    } catch (err) {
      showErrorToast("Failed to copy URL")
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* A2A Integration Card */}
        <Card>
          <CardHeader>
            <div className="flex items-start justify-between">
              <div className="space-y-1.5">
                <CardTitle>A2A Integration</CardTitle>
                <CardDescription>
                  Enable Agent-to-Agent protocol for external agent communication
                </CardDescription>
              </div>
              <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
                <div className="relative">
                  <input
                    type="checkbox"
                    checked={a2aEnabled}
                    onChange={(e) => handleA2aEnabledChange(e.target.checked)}
                    disabled={updateA2aConfigMutation.isPending}
                    className="sr-only"
                  />
                  <div
                    className={`block h-6 w-11 rounded-full transition-colors ${
                      a2aEnabled ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                    }`}
                  ></div>
                  <div
                    className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                      a2aEnabled ? "translate-x-5" : ""
                    }`}
                  ></div>
                </div>
              </label>
            </div>
          </CardHeader>
          <CardContent>
            {a2aEnabled && (
              <div className="space-y-3">
                <div>
                  <p className="text-sm font-medium mb-2">Agent Card URL</p>
                  <div className="flex gap-2">
                    <Input
                      value={a2aUrl}
                      readOnly
                      className="font-mono text-sm"
                    />
                    <Button
                      variant="outline"
                      size="icon"
                      onClick={handleCopyA2aUrl}
                      title="Copy URL"
                    >
                      {copiedUrl ? (
                        <Check className="h-4 w-4 text-green-500" />
                      ) : (
                        <Copy className="h-4 w-4" />
                      )}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    Use this URL to connect external A2A-compatible clients
                  </p>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
        {/* Access Tokens Card */}
        <AccessTokensCard agentId={agent.id} />

        {/* Email Integration Card - Half width, only for non-clone agents */}
        {!agent.is_clone && (
          <EmailIntegrationCard agentId={agent.id} />
        )}
      </div>
    </div>
  )
}
