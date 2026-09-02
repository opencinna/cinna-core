import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Copy, Laptop } from "lucide-react"
import { useState } from "react"

import { ServerConfigService, type ServerConfigUpdate } from "@/client"
import { localAgentKitStartUrl } from "@/components/Common/CopyPromptSnippet"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

/**
 * Instance switch for the public, unauthenticated Local Agent Kit surface.
 *
 * Sits next to the Disclaimer card because both are instance-wide interface
 * decisions on the same singleton row, and shares its mutation and query key —
 * the update endpoint takes one partial payload, so two cards writing different
 * fields cannot conflict as long as they invalidate the same cache entry.
 */
export function LocalAgentKitCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)

  const { data: config, isLoading } = useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
  })

  const updateMutation = useMutation({
    mutationFn: (data: ServerConfigUpdate) =>
      ServerConfigService.updateServerConfig({ requestBody: data }),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ["serverConfig"] })
      // The other surfaces gate on the public probe, whose cached answer is now
      // wrong for this session — drop it so the admin sees a consistent app.
      queryClient.invalidateQueries({ queryKey: ["localAgentKitVersion"] })
      showSuccessToast(
        updated.local_agent_kit_enabled
          ? "Local-agent starter is now public"
          : "Local-agent starter is no longer published",
      )
    },
    onError: () => showErrorToast("Failed to update the local agent starter"),
  })

  // Default true, matching the column: an instance nobody configured publishes.
  const enabled = config?.local_agent_kit_enabled ?? true
  const startUrl = localAgentKitStartUrl()

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(startUrl)
      setCopied(true)
      showSuccessToast("Starter URL copied")
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Laptop className="h-5 w-5" />
              Public local-agent starter (
              <code className="text-sm">/start</code>)
            </CardTitle>
            <CardDescription>
              Serves a read-only starter kit to anyone&rsquo;s coding assistant
              so they can build agents locally before signing up. No account, no
              user data — static content only. Turn it off to stop publishing it
              from this instance.
            </CardDescription>
          </div>
          <div className="ml-4 mt-1">
            <Switch
              checked={enabled}
              onCheckedChange={(checked) =>
                updateMutation.mutate({ local_agent_kit_enabled: checked })
              }
              disabled={isLoading || updateMutation.isPending}
              aria-label="Enable the public local-agent starter"
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-sm font-medium">Starter URL</p>
        <div className="flex gap-2">
          <Input value={startUrl} readOnly className="font-mono text-xs" />
          <Button
            variant="outline"
            size="icon"
            className="shrink-0"
            onClick={handleCopy}
            title="Copy starter URL"
            aria-label="Copy the starter URL"
          >
            {copied ? (
              <Check className="h-4 w-4 text-green-500" />
            ) : (
              <Copy className="h-4 w-4" />
            )}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          {enabled
            ? "Open it to check your reverse proxy routes /agent-start to the backend. The /api/agent-start alias always works, even where it does not."
            : "Disabled — every path under /agent-start and /api/agent-start returns 404 on this instance."}
        </p>
      </CardContent>
    </Card>
  )
}
