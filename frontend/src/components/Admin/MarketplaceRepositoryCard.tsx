import { GitBranch, RefreshCw } from "lucide-react"

import type { LLMPluginMarketplacePublic, MarketplaceStatus } from "@/client"
import {
  CopyValueButton,
  Fact,
} from "@/components/Admin/MarketplacePlugin/PluginFacts"
import type { RowStatusTone } from "@/components/Common/ListRow"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { StatusDot } from "@/components/Common/StatusDot"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { useMarketplaceSync } from "@/hooks/useMarketplaceSync"
import { marketplaceFormatLabel, plural } from "@/utils/marketplace"

const STATUS_COPY: Record<
  MarketplaceStatus,
  { label: string; tone: RowStatusTone }
> = {
  connected: { label: "Connected", tone: "on" },
  pending: { label: "Pending", tone: "off" },
  error: { label: "Error", tone: "error" },
  disconnected: { label: "Disconnected", tone: "off" },
}

/**
 * S13 — the Git repository this marketplace syncs from, and the one action
 * that re-reads it. Visibility moved to the Access card beside it.
 */
export function MarketplaceRepositoryCard({
  marketplace,
  marketplaceId,
}: {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
}) {
  // Shared with the header menu and the marketplaces list's row menu.
  const syncMutation = useMarketplaceSync(marketplaceId)
  const status = STATUS_COPY[marketplace.status] ?? STATUS_COPY.disconnected
  const commit = marketplace.sync_commit_hash || null
  const owner = marketplace.owner_name || marketplace.owner_email

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <GitBranch className="h-5 w-5" />
          Repository
        </CardTitle>
        <CardDescription>
          The Git repository this marketplace syncs from.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <Fact label="Repository URL" className="col-span-2">
            <span className="font-mono break-all">{marketplace.url}</span>
          </Fact>
          <Fact label="Branch">{marketplace.git_branch}</Fact>
          <Fact label="Format">{marketplaceFormatLabel(marketplace.type)}</Fact>
          <Fact label="Status">
            <StatusDot tone={status.tone} label={status.label} />
          </Fact>
          <Fact label="Entries">
            {plural(marketplace.plugin_count ?? 0, "entry", "entries")}
          </Fact>
          <Fact label="Last sync">
            {marketplace.last_sync_at ? (
              <RelativeTime timestamp={marketplace.last_sync_at} showTooltip />
            ) : (
              "Never"
            )}
          </Fact>
          {commit && (
            <Fact label="Last commit">
              <CopyValueButton
                value={commit}
                display={commit.substring(0, 8)}
                noun="Commit hash"
              />
            </Fact>
          )}
          {marketplace.ssh_key_id && <Fact label="SSH key">Configured</Fact>}
        </div>

        {owner && (
          <Fact label="Owner" className="border-t pt-4">
            {marketplace.owner_name}
            {marketplace.owner_name && marketplace.owner_email && " "}
            {marketplace.owner_email && (
              <span className="text-muted-foreground">
                {marketplace.owner_name
                  ? `(${marketplace.owner_email})`
                  : marketplace.owner_email}
              </span>
            )}
          </Fact>
        )}

        {marketplace.description && (
          <Fact label="Description" className="border-t pt-4">
            <span className="break-words">{marketplace.description}</span>
          </Fact>
        )}

        {marketplace.status_message && (
          <p className="rounded-md bg-muted p-3 text-sm break-words">
            {marketplace.status_message}
          </p>
        )}

        <div className="flex justify-end border-t pt-4">
          <Button
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
          >
            <RefreshCw
              className={
                syncMutation.isPending
                  ? "mr-2 h-4 w-4 animate-spin"
                  : "mr-2 h-4 w-4"
              }
            />
            {syncMutation.isPending ? "Syncing…" : "Sync Marketplace"}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
