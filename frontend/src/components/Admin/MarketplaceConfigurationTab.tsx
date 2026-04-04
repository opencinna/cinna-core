import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  GitBranch,
  Check,
  X,
  Clock,
  AlertCircle,
  RefreshCw,
  Key,
  Globe,
} from "lucide-react"

import type { LLMPluginMarketplacePublic } from "@/client"
import { LlmPluginsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label as UILabel } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

interface MarketplaceConfigurationTabProps {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
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

export function MarketplaceConfigurationTab({
  marketplace,
  marketplaceId,
}: MarketplaceConfigurationTabProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()

  const syncMutation = useMutation({
    mutationFn: () => LlmPluginsService.syncMarketplace({ marketplaceId }),
    onSuccess: () => {
      showSuccessToast("Marketplace synced successfully")
      queryClient.invalidateQueries({ queryKey: ["marketplace", marketplaceId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to sync marketplace")
    },
  })

  const updateMutation = useMutation({
    mutationFn: (public_discovery: boolean) =>
      LlmPluginsService.updateMarketplace({
        marketplaceId,
        requestBody: { public_discovery },
      }),
    onSuccess: () => {
      showSuccessToast("Marketplace visibility updated")
      queryClient.invalidateQueries({ queryKey: ["marketplace", marketplaceId] })
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update marketplace")
    },
  })

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Marketplace Configuration</CardTitle>
          <CardDescription>Git repository settings and status</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <Label>Repository URL</Label>
            <p className="text-sm font-mono mt-1 break-all">{marketplace.url}</p>
          </div>
          <div>
            <Label>Branch</Label>
            <div className="flex items-center gap-1 mt-1">
              <GitBranch className="h-3 w-3" />
              <span className="text-sm">{marketplace.git_branch}</span>
            </div>
          </div>
          <div>
            <Label>SSH Key</Label>
            <div className="flex items-center gap-1 mt-1">
              {marketplace.ssh_key_id ? (
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
            <Label>Type</Label>
            <Badge variant="secondary" className="mt-1">
              {marketplace.type}
            </Badge>
          </div>
          <div>
            <Label>Status</Label>
            <div className="mt-1">
              <StatusBadge status={marketplace.status} />
            </div>
          </div>
          <div>
            <Label>Plugin Count</Label>
            <p className="text-sm mt-1">{marketplace.plugin_count ?? 0} plugins</p>
          </div>
          <div>
            <Label>Last Sync</Label>
            <p className="text-sm text-muted-foreground mt-1">
              {marketplace.last_sync_at
                ? new Date(marketplace.last_sync_at).toLocaleString()
                : "Never"}
            </p>
          </div>
          {marketplace.sync_commit_hash && (
            <div>
              <Label>Last Commit</Label>
              <p className="text-sm font-mono text-muted-foreground mt-1">
                {marketplace.sync_commit_hash.substring(0, 8)}
              </p>
            </div>
          )}
        </div>

        {marketplace.owner_name || marketplace.owner_email ? (
          <div className="pt-4 border-t">
            <Label>Owner</Label>
            <p className="text-sm mt-1">
              {marketplace.owner_name}
              {marketplace.owner_email && (
                <span className="text-muted-foreground ml-1">
                  ({marketplace.owner_email})
                </span>
              )}
            </p>
          </div>
        ) : null}

        {marketplace.description && (
          <div className="pt-4 border-t">
            <Label>Description</Label>
            <p className="text-sm mt-1">{marketplace.description}</p>
          </div>
        )}

        {marketplace.status_message && (
          <div className="p-3 bg-muted rounded-md">
            <p className="text-sm">{marketplace.status_message}</p>
          </div>
        )}

        <div className="flex items-center justify-between pt-4 border-t">
          <div className="flex items-center gap-2">
            <Globe className="h-4 w-4 text-muted-foreground" />
            <UILabel htmlFor="public_discovery" className="text-sm cursor-pointer">
              Public
            </UILabel>
            <Switch
              id="public_discovery"
              checked={marketplace.public_discovery}
              onCheckedChange={(checked) => updateMutation.mutate(checked)}
              disabled={updateMutation.isPending}
            />
          </div>
          <Button
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
          >
            {syncMutation.isPending ? (
              <>
                <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                Syncing...
              </>
            ) : (
              <>
                <RefreshCw className="mr-2 h-4 w-4" />
                Sync Marketplace
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
