import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import {
  Bot,
  Share2,
  AlertCircle,
  Loader2,
  Network,
  Globe,
  Mail,
  Unplug,
  Webhook,
  Waypoints,
  Package,
} from "lucide-react"
import type { ComponentType, MouseEvent } from "react"

import type { AgentPublic, AgentStatusPublic } from "@/client"
import { InstallsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getColorPreset } from "@/utils/colorPresets"
import { AgentStatusCardFooter } from "./AgentStatusCardFooter"

interface AgentCardProps {
  agent: AgentPublic
  status?: AgentStatusPublic | null
}

export function AgentCard({ agent, status }: AgentCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const colorPreset = getColorPreset(agent.ui_color_preset)
  const hasStatusFooter =
    !!status && (status.severity != null || status.raw != null)

  // Capability badges summarise the agent's "purpose" on the card. When the
  // agent exposes any integration, these replace the entrypoint-prompt preview.
  const a2aEnabled =
    !!(agent.a2a_config as { enabled?: boolean } | null)?.enabled
  const capabilityBadges: { label: string; icon: ComponentType<{ className?: string }> }[] = [
    ...(agent.bundle_uuid && agent.is_publisher_install
      ? [{ label: "Bundle", icon: Package }]
      : []),
    ...(agent.agent_api_enabled ? [{ label: "API", icon: Network }] : []),
    ...(agent.webapp_enabled ? [{ label: "Web App", icon: Globe }] : []),
    ...(agent.has_email_integration ? [{ label: "Email", icon: Mail }] : []),
    ...(agent.has_mcp_connectors ? [{ label: "MCP", icon: Unplug }] : []),
    ...(agent.has_webhooks ? [{ label: "Webhooks", icon: Webhook }] : []),
    ...(a2aEnabled ? [{ label: "A2A", icon: Waypoints }] : []),
  ]

  const applyUpdate = useMutation({
    mutationFn: () => InstallsService.applyUpdate({ agentId: agent.id }),
    onSuccess: () => {
      showSuccessToast("Update applied")
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to apply update")
    },
  })

  const handleApplyUpdate = (e: MouseEvent<HTMLButtonElement>) => {
    e.preventDefault()
    e.stopPropagation()
    applyUpdate.mutate()
  }

  return (
    <Card
      className={cn(
        "relative transition-all hover:shadow-md hover:-translate-y-0.5 h-full flex flex-col gap-0 overflow-hidden",
        (hasStatusFooter || agent.pending_update) && "pb-0",
      )}
    >
      <Link
        to="/agent/$agentId"
        params={{ agentId: agent.id }}
        className="flex-1 flex flex-col cursor-pointer"
      >
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div className={`rounded-lg p-2 ${colorPreset.iconBg}`}>
              <Bot className={`h-5 w-5 ${colorPreset.iconText}`} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-start justify-between gap-1">
                <CardTitle className="text-lg break-words">
                  {agent.name}
                </CardTitle>
                {/* Bundle / share indicator stays next to the icon; the
                    "Update" badge moves to the card footer below. */}
                {agent.bundle_uuid && !agent.is_publisher_install && (
                  <Share2 className="h-3.5 w-3.5 text-muted-foreground shrink-0 mt-1.5" />
                )}
              </div>
            </div>
          </div>
        </CardHeader>
        {capabilityBadges.length > 0 ? (
          <CardContent className="pt-0 flex-1 min-h-0">
            <div className="flex flex-wrap gap-1.5">
              {capabilityBadges.map(({ label, icon: Icon }) => (
                <Badge key={label} variant="secondary" className="gap-1.5">
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </Badge>
              ))}
            </div>
          </CardContent>
        ) : (
          agent.entrypoint_prompt && (
            <CardContent className="pt-0 flex-1 min-h-0">
              <pre className="text-xs bg-muted/50 rounded-md p-3 overflow-hidden whitespace-pre-wrap break-words font-mono line-clamp-4">
                {agent.entrypoint_prompt}
              </pre>
            </CardContent>
          )
        )}
      </Link>
      {agent.pending_update && (
        <button
          type="button"
          aria-label="Apply available update"
          onClick={handleApplyUpdate}
          disabled={applyUpdate.isPending}
          className="mt-auto flex w-full items-center gap-1.5 border-t border-amber-200 bg-amber-50 px-3 py-2 text-left text-xs font-medium text-amber-700 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-400 dark:hover:bg-amber-950/50"
        >
          {applyUpdate.isPending ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
          ) : (
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          )}
          {applyUpdate.isPending ? "Updating…" : "Update available"}
        </button>
      )}
      {status && <AgentStatusCardFooter agentId={agent.id} status={status} />}
    </Card>
  )
}
