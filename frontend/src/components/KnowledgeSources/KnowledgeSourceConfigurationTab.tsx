import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  GitBranch,
  Check,
  X,
  Clock,
  AlertCircle,
  RefreshCw,
  Key,
} from "lucide-react"

import type { AIKnowledgeGitRepoPublic as KnowledgeSourceRead } from "@/client"
import { KnowledgeSourcesService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

interface KnowledgeSourceConfigurationTabProps {
  source: KnowledgeSourceRead
  sourceId: string
}

function StatusBadge({ status }: { status: string }) {
  const variants: Record<string, { icon: any; className: string; label: string }> = {
    connected: {
      icon: Check,
      className: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300",
      label: "Connected",
    },
    pending: {
      icon: Clock,
      className: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-300",
      label: "Pending",
    },
    error: {
      icon: AlertCircle,
      className: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300",
      label: "Error",
    },
    disconnected: {
      icon: X,
      className: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-300",
      label: "Disconnected",
    },
  }

  const variant = variants[status] || variants.disconnected
  const Icon = variant.icon

  return (
    <Badge className={variant.className} variant="outline">
      <Icon className="mr-1 h-3 w-3" />
      {variant.label}
    </Badge>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-sm font-medium text-muted-foreground">{children}</div>
}

export function KnowledgeSourceConfigurationTab({
  source,
  sourceId,
}: KnowledgeSourceConfigurationTabProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()

  const toggleEnabledMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      enabled
        ? KnowledgeSourcesService.enableKnowledgeSource({ sourceId })
        : KnowledgeSourcesService.disableKnowledgeSource({ sourceId }),
    onSuccess: (_, enabled) => {
      showSuccessToast(
        enabled
          ? "Source is now active and available for queries"
          : "Source is now inactive"
      )
      queryClient.invalidateQueries({ queryKey: ["knowledge-source", sourceId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update source")
    },
  })

  const checkAccessMutation = useMutation({
    mutationFn: () => KnowledgeSourcesService.checkKnowledgeSourceAccess({ sourceId }),
    onSuccess: (result) => {
      if (result.accessible) {
        showSuccessToast(result.message)
      } else {
        showErrorToast(result.message)
      }
      queryClient.invalidateQueries({ queryKey: ["knowledge-source", sourceId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to check access")
    },
  })

  const refreshMutation = useMutation({
    mutationFn: () => KnowledgeSourcesService.refreshKnowledgeSource({ sourceId }),
    onSuccess: (result) => {
      showSuccessToast(result.message)
      queryClient.invalidateQueries({ queryKey: ["knowledge-source", sourceId] })
      queryClient.invalidateQueries({ queryKey: ["knowledge-articles", sourceId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to refresh knowledge")
    },
  })

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Source Configuration</CardTitle>
            <CardDescription>Git repository settings and status</CardDescription>
          </div>
          <Switch
            checked={source.is_enabled}
            onCheckedChange={(checked) => toggleEnabledMutation.mutate(checked)}
            disabled={toggleEnabledMutation.isPending}
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <Label>Git URL</Label>
            <p className="text-sm font-mono mt-1">{source.git_url}</p>
          </div>
          <div>
            <Label>Branch</Label>
            <div className="flex items-center gap-1 mt-1">
              <GitBranch className="h-3 w-3" />
              <span className="text-sm">{source.branch}</span>
            </div>
          </div>
          <div>
            <Label>SSH Key</Label>
            <div className="flex items-center gap-1 mt-1">
              {source.ssh_key_id ? (
                <>
                  <Key className="h-3 w-3" />
                  <span className="text-sm">Configured</span>
                </>
              ) : (
                <span className="text-sm text-muted-foreground">None (public repo)</span>
              )}
            </div>
          </div>
          <div>
            <Label>Status</Label>
            <div className="mt-1">
              <StatusBadge status={source.status || "disconnected"} />
            </div>
          </div>
          <div>
            <Label>Last Sync</Label>
            <p className="text-sm text-muted-foreground mt-1">
              {source.last_sync_at
                ? new Date(source.last_sync_at).toLocaleString()
                : "Never"}
            </p>
          </div>
        </div>

        {source.status_message && (
          <div className="p-3 bg-muted rounded-md">
            <p className="text-sm">{source.status_message}</p>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-4">
          {source.status !== "connected" && (
            <Button
              variant="outline"
              onClick={() => checkAccessMutation.mutate()}
              disabled={checkAccessMutation.isPending}
            >
              {checkAccessMutation.isPending ? (
                <>
                  <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                  Checking...
                </>
              ) : (
                <>
                  <Check className="mr-2 h-4 w-4" />
                  Check Access
                </>
              )}
            </Button>
          )}
          <Button
            onClick={() => refreshMutation.mutate()}
            disabled={refreshMutation.isPending || !source.is_enabled}
          >
            {refreshMutation.isPending ? (
              <>
                <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                Refreshing...
              </>
            ) : (
              <>
                <RefreshCw className="mr-2 h-4 w-4" />
                Refresh Knowledge
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
